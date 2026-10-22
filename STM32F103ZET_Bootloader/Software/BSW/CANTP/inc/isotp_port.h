#ifndef ISOTP_PORT_H
#define ISOTP_PORT_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** @file isotp_port.h
 *  @brief ISO-TP(多帧传输协议)与 STM32 bxCAN 外设的粘合层。
 *
 *  典型用法：
 *      main.c    : IsotpPort_Init();              // 配过滤器 + 启动 CAN + 开中断
 *      任务/中断 : IsotpPort_OnCanFrame(...)       // 由 HAL CAN RX 回调调用
 *      周期任务  : IsotpPort_Poll();               // 处理 Cr 定时器
 *                 IsotpPort_TakeRx(buf, len);     // 取走一整帧重组后的数据
 */

/** 本节点发送用的标准帧 ID(请求) */
#ifndef ISOTP_PORT_TX_ID
#define ISOTP_PORT_TX_ID          0x736U
#endif

/** 本节点接收用的标准帧 ID(响应，约定为请求 ID + 1) */
#ifndef ISOTP_PORT_RX_ID
#define ISOTP_PORT_RX_ID          0x737U
#endif

/** 重组缓冲大小(字节)。
 *
 *  这决定了「能收多长的报文」：库在收到首帧时会用声明长度和它比较，
 *  超过即返回 OVERFLOW 并丢弃整条报文(见 isotp.c: payload_length >
 *  link->receive_buf_size)。
 *
 *  ISO-TP 的长度字段是 12 位，普通报文上限 4095 字节，所以 4096 就能覆盖
 *  经典 ISO-TP 的全部范围。应用侧的大缓冲必须 <= 这个值。
 */
#ifndef ISOTP_PORT_BUF_SIZE
#define ISOTP_PORT_BUF_SIZE       4096U
#endif

/**
 * @brief 整条报文重组完成时回调，把数据交给应用。
 *
 *  运行在 CAN 接收中断上下文(最后一个 CF 进来的那一次中断里)，必须尽快返回，
 *  不要在里面做耗时操作或阻塞式发送。
 *
 * @param[in] data      完整报文，仅在本次调用期间有效(指向库的重组缓冲)，
 *                     库会在返回后立刻复用它，需要留存请自行拷贝。
 * @param[in] size      完整长度(<= ISOTP_PORT_BUF_SIZE)
 * @param[in] user_arg  注册时传入的值
 */
typedef void (*IsotpPort_RxHandler)(const uint8_t *data, uint16_t size, void *user_arg);

/**
 * @brief 注册「重组完成」回调。
 *
 *  注册后不再需要 IsotpPort_TakeRx() 轮询：回调会在中断里直接把整条报文
 *  交给应用(少一次拷贝)。回调里拷进应用自己的大缓冲即可。
 *
 *  @param[in] cb 回调，传 NULL 可注销并退回 IsotpPort_TakeRx() 轮询模式。
 * @param[in] arg 透传给回调的值，可为 NULL。
 * @return 0 成功；-1 link 指针为空(不会发生)
 */
int IsotpPort_SetRxHandler(IsotpPort_RxHandler cb, void *arg);

/**
 * @brief 初始化过滤器、启动 CAN、使能接收中断并初始化 ISO-TP 链路。
 * @note  必须在 HAL_CAN_Init() 之后、启动调度器之前调用。
 */
int IsotpPort_Init(void);

/**
 * @brief 发送一条 ISO-TP 报文(>8 字节会自动分段，由库完成)。
 * @warning 只能在任务上下文调用(内部会阻塞等待发送完成)。
 */
int IsotpPort_Send(const uint8_t *data, uint16_t size);

/**
 * @brief 周期调用，驱动 ISO-TP 的 Cr(等待流控帧)超时定时器。
 * @note  必须从任务里周期调用(建议 >= 1ms)，否则接收端会一直等流控帧而超时。
 */
void IsotpPort_Poll(void);

/**
 * @brief 取走一整帧已重组完成的报文。
 * @param[out] out     接收缓冲区
 * @param[in]  max_len out 的容量
 * @return 实际拷贝的字节数；返回 0 表示当前没有完整报文。
 */
uint16_t IsotpPort_TakeRx(uint8_t *out, uint16_t max_len);

/**
 * @brief 把一帧 CAN 报文交给 ISO-TP 状态机。
 * @note   由 HAL_CAN_RxFifo0MsgPendingCallback() 调用，可运行在中断上下文。
 * @return 1 = 该帧被接受；0 = ID 不匹配，已丢弃
 */
int IsotpPort_OnCanFrame(uint32_t std_id, const uint8_t *data, uint8_t len);

#ifdef __cplusplus
}
#endif

#endif /* ISOTP_PORT_H */
