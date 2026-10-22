#include "uart_user.h"
#include "usart.h"

/* MicroLIB(工程已勾选 Use MicroLIB)的 printf 直接调用 fputc，没有半主机(semihosting)概念；
   完整库则必须关掉半主机，否则链接期会报
   "L6915E: __use_no_semihosting was requested, but _sys_open was referenced"。
   两种配置下 fputc 都是标准库唯一的输出出口，所以下面的重定向对两者都有效。 */
#if !defined(__MICROLIB)
#pragma import(__use_no_semihosting)
#endif

#define UART_PRINTF_TIMEOUT 100U

void UART_Printf_Init(void)
{
  setvbuf(stdout, NULL, _IONBF, 0U);
}

/* 标准库所有输出的最终出口，重定向到 USART1(PA9) */
int fputc(int ch, FILE *stream)
{
  (void)stream;

  if (ch == EOF)
  {
    return EOF;
  }

  HAL_UART_Transmit(&huart1, (const uint8_t *)&ch, 1U, UART_PRINTF_TIMEOUT);

  return ch;
}

#if !defined(__MICROLIB)
/* ARM Compiler 6 兼容入口 */
int __io_putchar(int ch)
{
  return fputc(ch, stdout);
}

/* 完整库要求的半主机替代符号 */
void _ttywrch(int ch)
{
  (void)fputc(ch, stdout);
}

void _sys_exit(int return_code)
{
  (void)return_code;
  while (1)
  {
  }
}
#endif

