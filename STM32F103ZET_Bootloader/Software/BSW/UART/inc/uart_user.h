#ifndef _UART_USER_H_
#define _UART_USER_H_

#include <stdint.h>
#include <stdio.h>

/* 把 printf 等标准库的输出重定向到 USART1。
   在使用 printf 前调用一次 UART_Printf_Init() 即可（可选，仅为显式初始化）。 */
void UART_Printf_Init(void);

#endif
