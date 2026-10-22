#include "isotp_port.h"

#include <string.h>

#include "isotp.h"
#include "isotp_defines.h"
#include "isotp_user.h"

#include "can.h"
#include "main.h"

#include "FreeRTOS.h"
#include "task.h"

/* ==================== ISO-TP 链路与缓冲区 ==================== */

static IsoTpLink s_link;
static uint8_t   s_tx_buf[ISOTP_PORT_BUF_SIZE];
static uint8_t   s_rx_buf[ISOTP_PORT_BUF_SIZE];

/* ==================== 重组完成 -> 应用层 的单槽邮箱 ====================
 *
 * ISO-TP 接收完成回调运行在调用 isotp_on_can_message() 的上下文里(本工程是
 * CAN 中断)，而消费方是任务。用「序列号 + 临界区拷贝」的无锁单槽邮箱交接：
 *   - 中断: 先 memcpy 填满缓冲区，最后才 ++seq  (读者看到 seq 变化即数据已就绪)
 *   - 任务: 进入临界区后拷贝并推进 seen         (中断无法在拷贝中途抢走缓冲区)
 * 溢出时覆盖最旧的一帧——对诊断报文可以接受，且绝不会读到半帧数据。
 */
static uint8_t           s_mail_buf[ISOTP_PORT_BUF_SIZE];
static volatile uint16_t s_mail_len;
static volatile uint8_t  s_mail_seq;   /* 生产者(ISR)写 */
static volatile uint8_t  s_mail_seen;  /* 消费者(任务)写 */

/* 应用注册的「整条报文完成」回调。为 NULL 时退回 IsotpPort_TakeRx() 轮询模式。 */
static IsotpPort_RxHandler s_rx_handler;
static void               *s_rx_handler_arg;

/* ISO-TP 重组完成回调，需在 IsotpPort_Init() 之前声明 */
static void IsotpPort_RxDoneCb(void *link, const uint8_t *data,
                               uint32_t size, void *user_arg);

/**
 * @brief 排空 FIFO0 中的所有报文并逐帧交给 ISO-TP 状态机。
 */
static void IsotpPort_RxIrqHandler(CAN_HandleTypeDef *p_hcan)
{
  CAN_RxHeaderTypeDef header;
  uint8_t              data[ISO_TP_MAX_CAN_FRAME_SIZE];

  while (HAL_CAN_GetRxMessage(p_hcan, CAN_RX_FIFO0, &header, data) == HAL_OK)
  {
    /* 只处理标准数据帧 */
    if ((header.IDE == CAN_ID_STD) && (header.RTR == CAN_RTR_DATA))
    {
      IsotpPort_OnCanFrame(header.StdId, data, (uint8_t)header.DLC);
    }
  }
}

/* ==================== HAL CAN 回调 ==================== */

void HAL_CAN_RxFifo0MsgPendingCallback(CAN_HandleTypeDef *p_hcan)
{
  IsotpPort_RxIrqHandler(p_hcan);
}

/* ==================== 对外接口 ==================== */

int IsotpPort_Init(void)
{
  CAN_FilterTypeDef sFilterConfig = {0};

  /* ---- 1) 硬件过滤器：只放行 RX ID 的标准帧到 FIFO0 ----
     32 位过滤器中标准 ID 位于 [31:21]，因此要左移 5 位。
     MaskLow 全 1 = 不关心 IDE/RTR/DLC，只比较 ID。 */
  sFilterConfig.FilterBank            = 0;
  sFilterConfig.FilterMode            = CAN_FILTERMODE_IDMASK;
  sFilterConfig.FilterScale           = CAN_FILTERSCALE_32BIT;
  sFilterConfig.FilterIdHigh          = (uint16_t)(ISOTP_PORT_RX_ID << 5);
  sFilterConfig.FilterIdLow           = 0x0000U;
  sFilterConfig.FilterMaskIdHigh      = (uint16_t)(0x7FFU << 5);
  sFilterConfig.FilterMaskIdLow       = 0xFFFFU;
  sFilterConfig.FilterFIFOAssignment  = CAN_RX_FIFO0;
  sFilterConfig.FilterActivation      = CAN_FILTER_ENABLE;
  sFilterConfig.SlaveStartFilterBank  = 0U;  /* F1 是单 CAN，无从节点概念 */

  if (HAL_CAN_ConfigFilter(&hcan, &sFilterConfig) != HAL_OK)
  {
    return -1;
  }

  /* ---- 2) 启动 CAN：清除 CAN_MCR.INRQ，退出初始化模式 ----
     不做这一步，CAN 永远不参与总线通信，TXRQ 请求不会被执行。 */
  if (HAL_CAN_Start(&hcan) != HAL_OK)
  {
    return -2;
  }

  /* ---- 3) 初始化 ISO-TP 链路并挂上接收完成回调 ---- */
  isotp_init_link(&s_link, ISOTP_PORT_TX_ID,
                  s_tx_buf, ISOTP_PORT_BUF_SIZE,
                  s_rx_buf, ISOTP_PORT_BUF_SIZE);
  isotp_set_rx_done_cb(&s_link, IsotpPort_RxDoneCb, NULL);

  s_mail_len  = 0U;
  s_mail_seq  = 0U;
  s_mail_seen = 0U;

    s_rx_handler     = NULL;
    s_rx_handler_arg = NULL;

  /* ---- 4) 开中断：FIFO0 有帧、发送邮箱空、以及各类总线错误 ---- */
  if (HAL_CAN_ActivateNotification(&hcan,
        CAN_IT_RX_FIFO0_MSG_PENDING | CAN_IT_TX_MAILBOX_EMPTY |
        CAN_IT_ERROR | CAN_IT_BUSOFF |
        CAN_IT_ERROR_PASSIVE | CAN_IT_LAST_ERROR_CODE) != HAL_OK)
  {
    return -3;
  }

  return 0;
}

int IsotpPort_OnCanFrame(uint32_t std_id, const uint8_t *data, uint8_t len)
{
  if (std_id != ISOTP_PORT_RX_ID)
  {
    return 0;
  }

  /* 库内部可能同步发流控帧，isotp_user_send_can() 走
     HAL_CAN_AddTxMessage()，在中断上下文调用是安全的。 */
  isotp_on_can_message(&s_link, data, len);

  return 1;
}

void IsotpPort_Poll(void)
{
  isotp_poll(&s_link);
}

int IsotpPort_Send(const uint8_t *data, uint16_t size)
{
  return isotp_send(&s_link, data, size);
}

uint16_t IsotpPort_TakeRx(uint8_t *out, uint16_t max_len)
{
  uint16_t len = 0U;
  uint8_t  seq;

  if (out == NULL)
  {
    return 0U;
  }

  taskENTER_CRITICAL();

  if (s_mail_seq != s_mail_seen)
  {
    seq        = s_mail_seq;
    len        = (s_mail_len > max_len) ? max_len : s_mail_len;

    if (len > 0U)
    {
      (void)memcpy(out, s_mail_buf, len);
    }

    s_mail_seen = seq;   /* 取走后才推进，保证 ISR 有机会覆盖下一整帧 */
  }

  taskEXIT_CRITICAL();

  return len;
}

/**
 * @brief ISO-TP 重组完成回调。
 * @param data 指向库的内部接收缓冲，只在本次调用期间有效，必须在此拷贝走。
 */
static void IsotpPort_RxDoneCb(void *link, const uint8_t *data,
                               uint32_t size, void *user_arg)
{
  (void)link;
  (void)user_arg;

  if ((data == NULL) || (size == 0U))
  {
    return;
  }

  if (size > (uint32_t)ISOTP_PORT_BUF_SIZE)
  {
    return;   /* 理论上不会发生：库已保证 receive_size <= receive_buf_size */
  }

  /* 优先走应用回调：直接把整条报文交给它，比经由邮箱少一次拷贝 */
  if (s_rx_handler != NULL)
  {
    s_rx_handler(data, (uint16_t)size, s_rx_handler_arg);
    return;
  }

  /* 未注册回调：退回轮询模型 */
  (void)memcpy(s_mail_buf, data, size);
  s_mail_len = (uint16_t)size;
  s_mail_seq++;   /* 最后推进：读者看到新 seq 就意味着数据已完整 */
}

int IsotpPort_SetRxHandler(IsotpPort_RxHandler cb, void *arg)
{
  if (s_link.send_buffer == NULL)
  {
    return -1;   /* 链路尚未初始化 */
  }

  s_rx_handler     = cb;
  s_rx_handler_arg = arg;

  return 0;
}
