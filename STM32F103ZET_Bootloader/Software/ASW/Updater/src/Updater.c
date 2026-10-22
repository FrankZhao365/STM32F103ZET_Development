#include "Updater.h"

#include "FreeRTOS.h"
#include "task.h"
#include "stmflash.h"   /* STMFLASH_Write() —— 写入内部 Flash */
#include "main.h"       /* RCC(跳转前复位外设时钟) 与 CMSIS 的 __set_MSP() */
#include "can.h"        /* hcan 与 CAN_TSR_TMx —— 跳转前等待响应发完 */
#include <string.h>

/* ==================== 报文接收缓冲 ====================
 *
 * 应用自有的大数组。ISO-TP 重组完成时由 Updater_OnIsoTpFrame() 在中断里
 * 一次性把整条报文拷贝进来；任务随后在 Updater_Poll() 里取走。
 */
uint8_t  DETECT_RX_BUFFER[UPDATER_RX_BUF_SIZE];
uint8_t  CAN_RX_BUF[CAN_REC_LEN];
uint8_t  DCM_BUFFER[256];
uint32_t FW_ADDRESS;
uint32_t FW_SIZE;
uint8_t  BLOCK_NUM = 0;
uint16_t Single_Len[10] = {0};
uint16_t Total_Len = 0;
uint8_t  FW_Rx_Confirmation = 0;
static volatile uint16_t s_rx_len;
static volatile uint8_t  s_rx_seq;   /* 生产者(中断)写 */
static uint8_t           s_rx_seen;  /* 消费者(任务)写 */

static Updater_StateTypeDef s_state = UPDATER_STATE_IDLE;

/* ==================== 中断侧：ISO-TP 完成回调 ====================
 *
 * 运行在 CAN 接收中断上下文(最后一个 CF 进来的那一次中断里)，必须尽快返回：
 * 不要做耗时操作，也不要阻塞式发送。
 */
static void Updater_OnIsoTpFrame(const uint8_t *data, uint16_t size, void *user_arg)
{
  uint16_t i;

  (void)user_arg;

  if ((data == NULL) || (size == 0U))
  {
    return;
  }

  if (size > UPDATER_RX_BUF_SIZE)
  {
    size = UPDATER_RX_BUF_SIZE;
  }

  /* 先拷完，最后才推进 seq：保证任务永远读不到半帧数据。
     逐字节拷贝而非 memcpy，是为了避免在中断里进入 libc(部分实现不可重入)；
     4KB 的循环在这个报文速率下完全够用。 */
  for (i = 0U; i < size; i++)
  {
    DETECT_RX_BUFFER[i] = data[i];
  }

  s_rx_len = size;
  s_rx_seq++;
}

/* ==================== 任务侧：观察 / 确认 ==================== */

/**
 * @brief 观察当前帧，不消费。
 *
 * 返回的指针在调用期间指向本模块缓冲。若处理期间又来新帧，中断会直接覆写
 * 这块内存(见 Updater_OnFrame 的警告)。
 */
const uint8_t *Updater_PeekFrame(uint16_t *out_len)
{
  if (s_rx_seq == s_rx_seen)
  {
    if (out_len != NULL)
    {
      *out_len = 0U;
    }
    return NULL;
  }

  if (out_len != NULL)
  {
    *out_len = s_rx_len;
  }

  return DETECT_RX_BUFFER;
}

/**
 * @brief 确认消费当前帧。只应在处理完成后调用。
 * @return 1 = 本帧已被消费；0 = 处理期间来过新帧，本帧已被覆盖(下一轮处理新帧)
 */
static int Updater_Consume(void)
{
  int consumed = 0;

  taskENTER_CRITICAL();

  if (s_rx_seq != s_rx_seen)
  {
    s_rx_seen = s_rx_seq;
    s_rx_len  = 0U;
    consumed  = 1;
  }

  taskEXIT_CRITICAL();

  return consumed;
}

/* ==================== 对外接口 ==================== */

int Updater_TakeFrameInto(uint8_t *dst, uint16_t dst_len, uint16_t *out_len)
{
  uint16_t len = 0U;
  uint16_t i;

  if (out_len != NULL)
  {
    *out_len = 0U;
  }

  if (dst == NULL)
  {
    return -1;
  }

  taskENTER_CRITICAL();

  if (s_rx_seq == s_rx_seen)
  {
    taskEXIT_CRITICAL();
    return 0;               /* 没有新报文 */
  }

  len = s_rx_len;
  if (dst_len < len)
  {
    taskEXIT_CRITICAL();
    return -1;              /* 目标太小，不消费，调用方可换大缓冲重试 */
  }

  for (i = 0U; i < len; i++)
  {
    dst[i] = DETECT_RX_BUFFER[i];
  }

  s_rx_seen = s_rx_seq;     /* 消费后才推进 */
  s_rx_len  = 0U;

  taskEXIT_CRITICAL();

  if (out_len != NULL)
  {
    *out_len = len;
  }

  return 1;
}

void Updater_Init(void)
{
  s_rx_len  = 0U;
  s_rx_seq  = 0U;
  s_rx_seen = 0U;
  s_state   = UPDATER_STATE_IDLE;

  /* 注册完成回调：之后整条报文会在中断里直接拷进 s_rx_buf，
     不再经过 isotp_port 内部的轮询邮箱，少一次拷贝。 */
  (void)IsotpPort_SetRxHandler(Updater_OnIsoTpFrame, NULL);
}

void Updater_Poll(void)
{
  const uint8_t *frame;
  uint16_t       len = 0U;

  frame = Updater_PeekFrame(&len);
  if (frame == NULL)
  {
    return;
  }

  /* 任务上下文，可以放心做耗时操作 */
  Updater_OnFrame(frame, len);

  /* 处理完才确认消费。若处理期间又来新帧，这里返回 0，
     下一轮 Updater_Poll() 会处理那帧新的数据。 */
  (void)Updater_Consume();
}

Updater_StateTypeDef Updater_GetState(void)
{
  return s_state;
}

/* ==================== 待实现：升级报文处理 ====================
 *
 * 由 Updater_Poll() 在任务上下文调用，可以做耗时操作(校验、写 Flash 等)。
 * 收到的整条报文在 data[0 .. data_len-1]。
 *
 * @warning data 指向本模块内部缓冲。处理期间如果又来一帧，中断会直接覆写这块
 *          内存。要允许「边处理边收下一帧」，需改成双缓冲(ping-pong)：
 *          中断写空闲的那块，处理完再切换。
 */
void Updater_OnFrame(const uint8_t *data, uint16_t data_len)
{
  /* UDS 请求至少 1 字节(SID)，空报文直接丢弃。
     注意不能写 &data[0] —— data_len 为 0 时那是对空缓冲取地址。 */
  if ((data == NULL) || (data_len == 0U))
  {
    s_state = UPDATER_STATE_ERROR;
    return;
  }

  s_state = UPDATER_STATE_RECEIVING;
  Message_Handler(data, data_len);
}
/**
 * @brief 等待 CAN 发送邮箱全部空闲，确保刚提交的响应帧已经真正发到总线上。
 *
 * IsotpPort_Send() 内部走 HAL_CAN_AddTxMessage()，它只把帧写进发送邮箱并
 * 置位 TXRQ 就返回，帧此时**还没上总线**。若紧接着复位 CAN 外设再跳转 APP，
 * 这一帧会被截断，对端表现为"没收到响应"。
 *
 * @param timeout_ms 最长等待时间(ms)，超时后无论如何都返回，避免死等。
 *
 * @note 必须运行在任务上下文(用 HAL_GetTick() 做超时)。
 */
static void Updater_WaitCanTxIdle(uint32_t timeout_ms)
{
    uint32_t start = HAL_GetTick();

    /* TME0/1/2 全部置位 = 三个发送邮箱都空出来了 = 最后一帧已发完 */
    while ((hcan.Instance->TSR & (CAN_TSR_TME0 | CAN_TSR_TME1 | CAN_TSR_TME2))
           != (CAN_TSR_TME0 | CAN_TSR_TME1 | CAN_TSR_TME2))
    {
        if ((HAL_GetTick() - start) > timeout_ms)
        {
            break;   /* 总线异常(无人 ACK / 断线)，不能再等下去 */
        }
    }

    /* 总线传输在 500kbps 下 8 字节约需 250us；
       从 TME 置位到对端采样完还有余量，这里再补一点余量。 */
    (void)HAL_Delay(2);
}

void Message_Handler(const uint8_t *data, uint16_t data_len)
{
    uint8_t Tx_Data[8]={0};
    uint16_t payload_len;

    /* 用局部变量接住 SID，不污染全局 SERVICE_TYPE：
       若后续要支持多路请求，全局变量会互相覆盖 */
    uint8_t service_type = data[0];

    switch(service_type)
    {
      case DiagnosticSessionControl:
      {
        /* 子功能占 1 字节，至少要 2 字节才有意义 */
        if (data_len < 2U)
        {
            /* UDS 规范：长度错误回 0x13 RequestOutOfRange */
            Tx_Data[0] = (uint8_t)(DiagnosticSessionControl + 0x40U);
            Tx_Data[1] = 0x13U;
            (void)IsotpPort_Send(&Tx_Data[0], 2);
            break;
        }

        if(data[1] == 0x01 ||
           data[1] == 0x02 ||
           data[1] == 0x03 )
        {
          Tx_Data[0] = (uint8_t)(DiagnosticSessionControl + 0x40U);
          Tx_Data[1] = data[1];
          (void)IsotpPort_Send(&Tx_Data[0], 2);
        }
  
      }
      break;

      case ECUReset:
      {
        /* UDS ECUReset 子功能：
           0x01 硬复位 / 0x02 钥匙off-on / 0x03 软复位 / 0x04 快速关机
           本工程只实现 0x01(硬复位 = 跳转到 APP)。 */
        if (data_len < 2U)
        {
            Tx_Data[0] = 0x7FU;
            Tx_Data[1] = ECUReset;
            Tx_Data[2] = 0x13U;   /* NRC: requestOutOfRange */
            (void)IsotpPort_Send(&Tx_Data[0], 3);
            break;
        }

        if (data[1] != 0x01U)
        {
            Tx_Data[0] = 0x7FU;
            Tx_Data[1] = ECUReset;
            Tx_Data[2] = 0x12U;   /* NRC: subFunctionNotSupported */
            (void)IsotpPort_Send(&Tx_Data[0], 3);
            break;
        }

        /* 1) 先回肯定响应 0x51 0x01。
              必须等这一帧真正发完再跳转 —— IsotpPort_Send() 只是把帧塞进
              CAN 邮箱并置 TXRQ，若立刻跳转，iap_load_app 里的
              RCC->APBxRSTR 会复位 CAN 外设，把这帧截断在对端看来就是丢包。 */
        Tx_Data[0] = (uint8_t)(ECUReset + 0x40U);
        Tx_Data[1] = 0x01U;
        (void)IsotpPort_Send(&Tx_Data[0], 2);

        Updater_WaitCanTxIdle(50U);

        /* 2) 跳转到 APP。iap_load_app 内部会校验 APP 区栈顶地址，
              若 APP 无效则直接返回，bootloader 继续等待下一轮升级。 */
        iap_load_app(FLASH_APP1_ADDR);

        /* 3) APP 无效时的兜底：停在 bootloader，不再往下执行。
              UDS 规范下这里应回 NRC 0x72，但此时无法可靠发包
              (APP 无效通常意味着 Flash 为空或损坏)。 */
        s_state = UPDATER_STATE_ERROR;
      }
      break;
      case SecurityAccess:
      {

      }
      break;
      case CommunicationControl:
      {

      }
      break;

      case RequestDownload:
      {
        memcpy(&DCM_BUFFER[0],&data[0],data_len);
        FW_ADDRESS = ((uint32_t)DCM_BUFFER[3])<<(24)|
                     ((uint32_t)DCM_BUFFER[4])<<(16)|
                     ((uint32_t)DCM_BUFFER[5])<<(8)|
                     ((uint32_t)DCM_BUFFER[6]);/*Get FW address*/
        FW_SIZE    = ((uint32_t)DCM_BUFFER[7])<<(24)|
                     ((uint32_t)DCM_BUFFER[8])<<(16)|
                     ((uint32_t)DCM_BUFFER[9])<<(8)|
                     ((uint32_t)DCM_BUFFER[10]);/*Get FW size*/
        Tx_Data[0] = (uint8_t)(RequestDownload + 0x40U);
        Tx_Data[1] = 0x00;
        Tx_Data[2] = (uint8_t)((BLOCK_SIZE & 0xFF00)>>(8));
        Tx_Data[3] = (uint8_t)(BLOCK_SIZE & 0x00FF);
        Total_Len = 0;
        /* ISO 14229-1 规定 TransferData 的 blockSequenceCounter
           **从 1 开始**递增(0x01,0x02,...)，到 0xFF 后回绕到 0x00。
           这里必须置 1，否则第一块就会被下面的序号检查拒掉。 */
        BLOCK_NUM = 1;
        FW_Rx_Confirmation = 0;
        (void)IsotpPort_Send(&Tx_Data[0], 4);
      }
      break;

      case TransferData:
      {
        /* UDS TransferData 格式：[0]=0x36 SID, [1]=块序号(BSC), [2..]=固件数据
           至少要有 SID + 块序号 2 字节 */
        if (data_len < 2U)
        {
            /* 负响应：[0]=0x7F, [1]=SID, [2]=NRC */
            Tx_Data[0] = 0x7FU;
            Tx_Data[1] = TransferData;
            Tx_Data[2] = 0x13U;   /* NRC: requestOutOfRange */
            (void)IsotpPort_Send(&Tx_Data[0], 3);
            break;
        }

        payload_len = (uint16_t)(data_len - 2U);

        /* 1) 缓冲越界检查：宁可不写也不能写穿 CAN_RX_BUF。
              RequestDownload 里声明的 FW_SIZE 也要一起校验，
              否则对端可以只发一个 RequestDownload 就把 RAM 撑爆。 */
        if ((FW_SIZE > (uint32_t)CAN_REC_LEN) ||
            (((uint32_t)Total_Len + (uint32_t)payload_len) > (uint32_t)CAN_REC_LEN) ||
            (((uint32_t)Total_Len + (uint32_t)payload_len) > FW_SIZE))
        {
            Tx_Data[0] = 0x7FU;
            Tx_Data[1] = TransferData;
            Tx_Data[2] = 0x72U;   /* NRC: memoryOverflow */
            FW_Rx_Confirmation = 0U;
            (void)IsotpPort_Send(&Tx_Data[0], 3);
            break;
        }

        /* 2) 块序号必须连续：UDS 要求对端按 BSC 递增发送，
              乱序或丢块说明传输出错，直接拒绝而不是默默写错位置。 */
        if (data[1] != BLOCK_NUM)
        {
            Tx_Data[0] = 0x7FU;
            Tx_Data[1] = TransferData;
            Tx_Data[2] = 0x73U;   /* NRC: wrongBlockSequenceCounter */
            FW_Rx_Confirmation = 0U;
            (void)IsotpPort_Send(&Tx_Data[0], 3);
            break;
        }

        /* 3) 按到达顺序追加到 CAN_RX_BUF 尾部。
              Total_Len 同时充当写指针，RequestDownload 已将其清零。 */
        (void)memcpy(&CAN_RX_BUF[Total_Len], &data[2], payload_len);

        /* Single_Len 只有 10 个元素，而 BLOCK_NUM 是 uint8_t(0~255)，
           必须做上界保护，否则写越界会踩坏后面的内存。
           注意 BLOCK_NUM 现在从 1 开始(BEC 递增)，有效索引是 1~9。 */
        if ((BLOCK_NUM > 0U) &&
            (BLOCK_NUM < (uint8_t)(sizeof(Single_Len) / sizeof(Single_Len[0]))))
        {
            Single_Len[BLOCK_NUM] = payload_len;
        }

        Total_Len = (uint16_t)(Total_Len + payload_len);

        /* BSC 按 UDS 规范递增并回绕：...0xFE, 0xFF, 0x00, 0x01... */
        BLOCK_NUM = (uint8_t)(BLOCK_NUM + 1U);

        /* 4) 正响应：回肯定响应 SID 和收到的块序号 */
        Tx_Data[0] = (uint8_t)(TransferData + 0x40U);
        Tx_Data[1] = data[1];
        (void)IsotpPort_Send(&Tx_Data[0], 2);
      }
      break;
      case RequestTransferExit:
      {
        Tx_Data[0] = (uint8_t)(RequestTransferExit + 0x40U);
        Tx_Data[1] = 0x01;
        (void)IsotpPort_Send(&Tx_Data[0], 2);
        FW_Rx_Confirmation = 1;
      }
      break;

      case TesterPresent:
      {

      }
      break;

      default:
      break;

    }
}

static uint16_t iapbuf[1024];
//appxaddr:应用程序的起始地址
//appbuf:应用程序CODE.
//appsize:应用程序大小(字节).
void iap_write_appbin(uint32_t appxaddr,uint8_t *appbuf,uint32_t appsize)
{
	uint16_t t;
	uint16_t i=0;
	uint16_t temp;
	uint32_t fwaddr=appxaddr;//当前写入的地址
	uint8_t *dfu=appbuf;

	/* 越界保护：没有这段，一个被篡改的 FW_SIZE 会把固件写到
	   APP 区之外，覆盖 bootloader 自身或直接跑飞。 */
	if ((appxaddr < FLASH_APP1_ADDR) ||
	    (appxaddr > (STM32_FLASH_BASE + (STM32_FLASH_SIZE * 1024U) - 1U)))
	{
		return;
	}

	if ((appsize == 0U) ||
	    (appsize > (STM32_FLASH_SIZE * 1024U) - (appxaddr - STM32_FLASH_BASE)))
	{
		return;
	}

	for(t=0;t<appsize;t+=2)
	{
		temp=(uint16_t)dfu[1]<<8;
		temp+=(uint16_t)dfu[0];
		dfu+=2;//偏移2个字节
		iapbuf[i++]=temp;
		if(i==1024)
		{
			i=0;
			STMFLASH_Write(fwaddr,iapbuf,1024);
			fwaddr+=2048;//偏移2048  16=2*8.所以要乘以2.
		}
	}
	if(i)STMFLASH_Write(fwaddr,iapbuf,i);//将最后的一些内容字节写进去.
}

/* ==================== 固件落盘 ==================== */

uint8_t Updater_IsFirmwareReady(void)
{
    return FW_Rx_Confirmation;
}

int Updater_FlushFirmware(void)
{
    /* 必须先清标志：Flash 擦写期间若发生复位(掉电、看门狗)，
       标志仍为 1 会导致重启后重复写入同一个扇区。 */
    FW_Rx_Confirmation = 0U;

    if ((FW_SIZE == 0U) ||
        (FW_SIZE > (uint32_t)CAN_REC_LEN) ||
        (FW_SIZE != (uint32_t)Total_Len))
    {
        return -1;
    }

    /* 写整个固件到 APP 区。
       注意：Flash 擦写期间 CPU 无法从 Flash 取指，这里会阻塞数百毫秒
       (30KB ≈ 15 个扇区 × ~40ms)。期间所有中断和任务都停摆。 */
    iap_write_appbin(FLASH_APP1_ADDR, &CAN_RX_BUF[0], FW_SIZE);

    /* 写完后复位，而不是在本函数里直接跳转：
       FreeRTOS 调度器仍在运行，直接跳 APP 会留下未释放的外设和中断，
       必须走复位 → bootloader 重新校验 → 再跳转这条干净路径。 */
    NVIC_SystemReset();

    return 0;   /* 不会执行到这里 */
}

//跳转到应用程序段
//appxaddr:用户代码起始地址.
void iap_load_app(uint32_t appxaddr)
{
	if(((*(volatile uint32_t*)appxaddr)&0x2FFE0000)==0x20000000)	//检查栈顶地址是否合法.
	{
		iapfun app_jump2app;

		/* 复位所有 APB1/APB2 外设，避免 APP 重配外设时旧状态残留。

		   注意：STM32F1 的 RCC 没有 AHB 总线复位寄存器(AHBRSTR 是 F2/F4 才有的)，
		   F1 的 AHB 上没有可复位外设(USB/CAN 都挂在 APB1)，所以只需复位 APB1/APB2。

		   关键：APBxRSTR 是写 1 置位、写 0 释放的保持型寄存器。复位外设后必须再写 0
		   释放复位，否则外设会一直停在复位状态。APP 的 LED_Init() 虽然使能了 GPIOB/
		   GPIOE 时钟并调用了 HAL_GPIO_Init()，但寄存器写不进去，表现为"能跳进 APP
		   但 LED 不亮"。 */
		RCC->APB2RSTR = 0xFFFFFFFFU;
		RCC->APB1RSTR = 0xFFFFFFFFU;
		RCC->APB2RSTR = 0x00000000U;
		RCC->APB1RSTR = 0x00000000U;

		/* 关闭所有外设时钟，APP 会按自己的需求重新使能 */
		RCC->APB2ENR = 0x00000000U;
		RCC->APB1ENR = 0x00000000U;

		/* 显式关闭所有外部中断使能：__disable_irq() 只是屏蔽，
		   并不会清除使能位，残留的 pending 位会在 APP 重配置期间持续触发。 */
		NVIC->ICER[0] = 0xFFFFFFFFU;
		NVIC->ICPR[0] = 0xFFFFFFFFU;

		/* 停掉 BootLoader(FreeRTOS)留下的 SysTick，APP 的 HAL_Init() 会重新配置。
		   若不清 LOAD，APP 里的 delay_us() 会按 BootLoader 的 reload 值计时而全错。 */
		SysTick->CTRL = 0U;
		SysTick->LOAD = 0U;
		SysTick->VAL  = 0U;

		__disable_irq();

		/* 先设置向量表再跳转：此时若发生中断，仍会取自 APP 的向量表。
		   APP 的 main() 里也会再设一次，双保险。 */
		SCB->VTOR = appxaddr;

		/* 从用户代码区第一个字取出栈顶地址并设置 MSP。
		   用内联汇编直接写 MSP 寄存器；不能写成普通函数调用。 */
		__set_MSP(*(volatile uint32_t *)appxaddr);

		/* 用户代码区第二个字是复位向量(程序入口)。
		   volatile 修饰是必须的：防止编译器认为该地址可达而删除跳转。 */
		app_jump2app = (iapfun)(*(volatile uint32_t *)(appxaddr + 4U));

		app_jump2app();								//跳转到APP.
	}
}

