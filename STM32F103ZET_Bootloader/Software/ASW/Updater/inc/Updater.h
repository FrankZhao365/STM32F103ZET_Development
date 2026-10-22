#ifndef _UPDATER_H_
#define _UPDATER_H_

#include <stdint.h>
#include "isotp_port.h"

#ifdef __cplusplus
extern "C" {
#endif

#define DiagnosticSessionControl    0x10 /*默认/编程/扩展/安全系统会话切换*/
#define ECUReset	                0x11 /*硬复位、钥匙复位、软复位*/
#define SecurityAccess	            0x27 /*seed-key 解锁*/
#define CommunicationControl	    0x28 /*开关非诊断报文收发*/
#define RequestDownload             0x34 /*	声明"我要往这段地址写这么多字节"，握手 + 协商分块大小*/
#define TransferData                0x36 /*真正搬数据的主循环，带块序号*/
#define RequestTransferExit         0x37 /*结束传输，通常在退出时顺带交校验和*/
#define TesterPresent	            0x3E /*心跳保活，非默认会话靠它撑着*/
#define BLOCK_SIZE	                0x0402
#define CAN_REC_LEN                 30*1024  		//定义最大接收字节数 55K
/** @file Updater.h
 *  @brief 固件升级(Updater)应用层。
 *
 *  职责边界：
 *    - isotp_port (BSW)：ISO-TP 协议状态机 + CAN 收发，不认识应用数据
 *    - Updater  (ASW)：持有收到的报文大数组、校验、升级流程
 *
 *  数据流：
 *    CAN RX0 中断 → ISO-TP 重组完成 → Updater_OnIsoTpFrame() 把整条报文拷进
 *    本模块的大数组 → BSW 任务里 Updater_Poll() 取走并交给 Updater_OnFrame()
 */

/** 应用接收缓冲大小。
 *
 *  直接引用 ISOTP_PORT_BUF_SIZE 而不另写常量：库在收到首帧时会用报文声明长度
 *  与重组缓冲比较，超过即判 OVERFLOW 丢弃整条报文
 *  (见 isotp.c: payload_length > link->receive_buf_size)。
 *  两处必须一致，否则会出现「数组很大但只能收很短报文」的情况。
 */
#define UPDATER_RX_BUF_SIZE   2048

/**
 * @brief 取走一帧完整报文并拷贝到调用者的缓冲。
 *
 * @param[out] dst     目标缓冲
 * @param[in]  dst_len dst 容量
 * @param[out] out_len 实际拷贝长度，可为 NULL
 * @return 1 = 取到并已拷贝；0 = 无数据；-1 = 目标缓冲太小(数据未取走，会被下一帧覆盖)
 */
int Updater_TakeFrameInto(uint8_t *dst, uint16_t dst_len, uint16_t *out_len);

/**
 * @brief 最近一帧完整报文的只读指针(不消费)。
 *
 * 返回的指针指向本模块内部缓冲，下一帧到达时会被覆盖；需要留存请自行拷贝。
 *
 * @param[out] out_len 长度，可为 NULL
 * @return 缓冲首地址；尚未收到过报文时返回 NULL
 */
const uint8_t *Updater_PeekFrame(uint16_t *out_len);

/** 当前状态机(便于调试和上位机查询)。 */
typedef enum {
    UPDATER_STATE_IDLE      = 0,  /**< 空闲，等待报文 */
    UPDATER_STATE_CHECKING  = 1,  /**< 正在校验帧头/长度 */
    UPDATER_STATE_RECEIVING = 2,  /**< 正在接收升级数据 */
    UPDATER_STATE_FINISHED  = 3,  /**< 升级数据接收完成 */
    UPDATER_STATE_ERROR     = 4   /**< 出错 */
} Updater_StateTypeDef;

Updater_StateTypeDef Updater_GetState(void);

/**
 * @brief 初始化 Updater：注册 ISO-TP 接收回调。
 * @note  必须在 CAN 通路(IsotpPort_Init())之后调用。
 */
void Updater_Init(void);

/**
 * @brief 周期调用：取走新到的完整报文并交给 Updater_OnFrame()。
 * @note  必须从任务上下文周期调用，建议 1ms~10ms。
 */
void Updater_Poll(void);

/**
 * @brief 收到一帧完整报文时的处理入口。
 *
 * 运行在任务上下文(由 Updater_Poll() 调用)，可以放心做耗时操作。
 *
 * @param[in] data     完整报文
 * @param[in] data_len 长度
 *
 * @warning 这就是你要填的地方：帧头校验、长度校验、写 Flash 等都放这里。
 */
void Updater_OnFrame(const uint8_t *data, uint16_t data_len);

/**
 * @brief UDS 诊断报文分发：按 SID(SERVICE_TYPE) 分发到各服务处理。
 *
 * @param[in] data     完整 UDS 报文，首字节为 SID
 * @param[in] data_len 长度
 */
void Message_Handler(const uint8_t *data, uint16_t data_len);

typedef  void (*iapfun)(void);				//定义一个函数类型的参数.

#define FLASH_APP1_ADDR		0x08010000  	//第一个应用程序起始地址(存放在FLASH)
											//保留0X08000000~0X0800FFFF的空间为IAP使用

void iap_load_app(uint32_t appxaddr);			//执行flash里面的app程序
void iap_load_appsram(uint32_t appxaddr);		//执行sram里面的app程序
void iap_write_appbin(uint32_t appxaddr,uint8_t *appbuf,uint32_t applen);	//在指定地址开始,写入bin

/**
 * @brief 固件是否已完整接收并通过长度校验。
 * @return 1 = RequestTransferExit 校验通过，可以写入 Flash；0 = 尚未就绪
 */
uint8_t Updater_IsFirmwareReady(void);

/**
 * @brief 把接收到的固件写入内部 Flash 的 APP 区。
 *
 * 内部会先清除就绪标志(防止写入途中复位后重复触发)，
 * 写入完成后复位 MCU —— 复位后由 bootloader 重新校验并跳转 APP。
 *
 * @warning Flash 擦写期间 CPU 无法从 Flash 取指，本调用会**阻塞数百毫秒**，
 *          期间无法响应任何中断或任务切换。固件越大阻塞越久。
 *
 * @return 0 = 已写入并已请求复位(正常情况下不会返回)；-1 = 参数非法，未写入
 */
int Updater_FlushFirmware(void);

#ifdef __cplusplus
}
#endif

#endif /* _UPDATER_H_ */
