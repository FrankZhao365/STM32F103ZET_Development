#include "isotp_user.h"
#include "isotp.h"
#include "isotp_defines.h"

#include "can.h"
#include "main.h"

#include <stdarg.h>
#include <stdio.h>

/* 发送 CAN 帧 */
int isotp_user_send_can(const uint32_t arbitration_id,
                        const uint8_t* data,
                        const uint8_t size)
{
    CAN_TxHeaderTypeDef TxHeader;
    uint32_t TxMailbox;

    TxHeader.StdId = arbitration_id;      // 如果是标准帧
    // TxHeader.ExtId = arbitration_id;   // 如果是扩展帧请改用这个
    TxHeader.IDE   = CAN_ID_STD;          // 或 CAN_ID_EXT
    TxHeader.RTR   = CAN_RTR_DATA;
    TxHeader.DLC   = size;
    TxHeader.TransmitGlobalTime = DISABLE;

    if (HAL_CAN_AddTxMessage(&hcan, &TxHeader, (uint8_t*)data, &TxMailbox) != HAL_OK)
    {
        return ISOTP_RET_ERROR;   // 发送失败
    }

    return ISOTP_RET_OK;
}

/* 获取微秒时间戳（用于超时判断） */
uint32_t isotp_user_get_us(void)
{
    // 使用 HAL 的微秒计数，或者用定时器
    // 简单方法：用 HAL_GetTick() * 1000（精度较低，但够用）
    return HAL_GetTick() * 1000;
    
    // 更精确的方法：使用一个 1us 定时器或 DWT 周期计数器
}

/* ==================== 调试输出 ====================
 *
 * isotp_user_debug() 会被 isotp_on_can_message() 调用，也就是运行在
 * CAN 接收中断上下文。115200 波特率下打印一行 40 字符要占用约 3.5ms
 * 的中断时间，会严重干扰 CAN 帧的接收(甚至触发错误帧)，所以这里绝不能
 * 直接发送串口。
 *
 * 做法：中断里只把文本塞进一个无锁环形缓冲并立即返回；由任务侧周期调用
 * IsotpUser_DebugPump() 真正打印出去。
 */

/** 调试输出总开关。置 0 可彻底裁掉这部分的代码和运行时开销。 */
#ifndef ISOTP_USER_DEBUG_ENABLE
#define ISOTP_USER_DEBUG_ENABLE  1
#endif

#if ISOTP_USER_DEBUG_ENABLE

/** 环形缓冲大小。取 2 的幂以免取模。 */
#define ISOTP_DBG_RING_SIZE  256U
#define ISOTP_DBG_RING_MASK  (ISOTP_DBG_RING_SIZE - 1U)

/** 单条调试信息的最长字符数，超出部分截断。 */
#define ISOTP_DBG_LINE_MAX   96U

static char           s_dbg_ring[ISOTP_DBG_RING_SIZE];
static volatile uint16_t s_dbg_head;   /* 生产者(中断)写 */
static volatile uint16_t s_dbg_tail;   /* 消费者(任务)写 */

/**
 * @brief 不可重入的临界区封送：兼容「任务里调用」和「中断里调用」两种情况。
 * @note  taskENTER_CRITICAL() 不能在中断中使用，这里直接用 PRIMASK 保证正确性。
 */
/* 注意：SAVE 必须展开成 do{...}while(0) 单语句块。
   若展开成「声明; 表达式;」两条语句，调用处再加分号就会让第二条变成裸表达式语句，
   ARMCC5 会报 #1-D「应输入分号」。变量也必须是块内局部量，作用域才覆盖 RESTORE。 */
#define ISOTP_DBG_IRQ_SAVE()          do { primask__ = __get_PRIMASK(); \
                                          __disable_irq(); } while (0)
#define ISOTP_DBG_IRQ_RESTORE()       do { if (primask__ == 0U) { __enable_irq(); } } while (0)

/**
 * @brief 把 ISO-TP 库的诊断信息入队。运行在中断上下文，立即返回。
 */
void isotp_user_debug(const char* message, ...)
{
    char      line[ISOTP_DBG_LINE_MAX];
        va_list   args;
        int       written;
        uint16_t  i;
        uint16_t  head;
        uint32_t  primask__;   /* 供 ISOTP_DBG_IRQ_SAVE/RESTORE 使用 */

    if (message == NULL)
    {
        return;
    }

    /* 先在栈上格式化完，再临界区入队，尽量缩短关中断的时间 */
    va_start(args, message);
    written = vsnprintf(line, sizeof(line), message, args);
    va_end(args);

    if (written <= 0)
    {
        return;
    }

    /* 截断到缓冲容量 */
    if (written > (int)ISOTP_DBG_LINE_MAX)
    {
        written = (int)ISOTP_DBG_LINE_MAX;
    }

    ISOTP_DBG_IRQ_SAVE();

    head = s_dbg_head;
    for (i = 0U; i < (uint16_t)written; i++)
    {
        s_dbg_ring[head & ISOTP_DBG_RING_MASK] = line[i];
        head++;

        /* 缓冲写满时挤掉最旧的一字节，避免生产者被卡死 */
        if ((uint16_t)(head - s_dbg_tail) > ISOTP_DBG_RING_SIZE)
        {
            s_dbg_tail++;
        }
    }
    s_dbg_head = head;

    ISOTP_DBG_IRQ_RESTORE();
}

/**
 * @brief 把缓冲里的调试信息发送到标准输出(已重定向到 USART1)。
 * @note  必须在任务上下文周期调用，建议与 isotp_poll() 同一周期。
 */
void IsotpUser_DebugPump(void)
{
    char     ch;
    uint32_t primask__;   /* 供 ISOTP_DBG_IRQ_SAVE/RESTORE 使用 */

    /* 无内容可取时直接返回，避免频繁关中断 */
    if (s_dbg_head == s_dbg_tail)
    {
        return;
    }

    for (;;)
    {
        ISOTP_DBG_IRQ_SAVE();

        if (s_dbg_head == s_dbg_tail)
        {
            ISOTP_DBG_IRQ_RESTORE();
            break;
        }

        ch = s_dbg_ring[s_dbg_tail & ISOTP_DBG_RING_MASK];
        s_dbg_tail++;

        ISOTP_DBG_IRQ_RESTORE();

        (void)fputc((int)(unsigned char)ch, stdout);
    }
}

#else  /* ISOTP_USER_DEBUG_ENABLE == 0 */

/* 裁剪版：空实现，库的错误路径不产生任何输出 */
void isotp_user_debug(const char* message, ...)
{
    (void)message;
}

void IsotpUser_DebugPump(void)
{
}

#endif /* ISOTP_USER_DEBUG_ENABLE */
