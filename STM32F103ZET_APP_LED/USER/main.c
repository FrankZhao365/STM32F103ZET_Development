#include "sys.h"
#include "delay.h"
#include "usart.h"
#include "led.h"
/************************************************
 ALIENTEK 精英STM32F103开发板 实验1
 跑马灯实验-HAL库版本
 技术支持：www.openedv.com
 淘宝店铺： http://eboard.taobao.com 
 关注微信公众平台微信号："正点原子"，免费获取STM32资料。
 广州市星翼电子科技有限公司  
 作者：正点原子 @ALIENTEK
************************************************/

/* APP在内部Flash中的起始地址，必须与BootLoader的FLASH_APP1_ADDR保持一致 */
#define APP_FLASH_START   0x08010000UL

/* BootLoader跳转现场恢复，必须在任何HAL初始化之前完成 */
static void APP_Startup(void)
{
    /* 1. 停掉BootLoader残留的SysTick，避免HAL_InitTick重配时计数错乱 */
    SysTick->CTRL = 0;
    SysTick->LOAD = 0;
    SysTick->VAL  = 0;

    /* 2. BootLoader跳转前执行了 RCC->APBxRSTR = 0xFFFFFFFF，把APB1/APB2上
          所有外设置于复位态。STM32F1的复位寄存器必须写0才释放复位，不清零的
          话GPIOB/GPIOE会一直停在复位状态，此时__HAL_RCC_GPIOx_CLK_ENABLE()使能
          了时钟、HAL_GPIO_Init()也执行了，但寄存器写不进去，LED就不会亮。*/
    RCC->APB2RSTR = 0x00000000U;
    RCC->APB1RSTR = 0x00000000U;

    /* 3. 中断向量表重定向到APP。BootLoader在0x08000000，APP在0x08010000，
          必须在恢复总中断之前设置，否则中断会跳到BootLoader的向量表上。*/
    SCB->VTOR = APP_FLASH_START;

    /* 4. BootLoader跳转前调用了__disable_irq()，APP必须自己恢复总中断，
          否则HAL基于中断的HAL_IncTick()等逻辑全部失效。*/
    __enable_irq();
}

/*下面主函数是使用HAL库函数实现控制IO口输出*/

int main(void)
{
    APP_Startup();
    HAL_Init();                    	 	//初始化HAL库    
    Stm32_Clock_Init(RCC_PLL_MUL9);   	//设置时钟,72M
	delay_init(72);               		//初始化延时函数
	LED_Init();							//初始化LED	
	
	while(1)
	{
        LED0=0;             	//LED0对应引脚PB5拉低，亮，等同于LED0(0)
        LED1=1;             	//LED1对应引脚PE5拉高，灭，等同于LED1(1)
        delay_ms(500);      	//延时500ms
        LED0=1;             	//LED0对应引脚PB5拉高，灭，等同于LED0(1)
        LED1=0;             	//LED1对应引脚PE5拉低，亮，等同于LED1(0)
        delay_ms(500);      	//延时500ms
	}
}

/*下面主函数使用位带操作实现：*/

/*int main(void)
{ 
    HAL_Init();                    	 	//初始化HAL库    
    Stm32_Clock_Init(RCC_PLL_MUL9);   	//设置时钟,72M
	delay_init(72);               			//初始化延时函数
	LED_Init();							//初始化LED	

	while(1)
	{
         LED0=0;			     	//LED0亮
	     LED1=1;				 	//LED1灭
		 delay_ms(500);
		 LED0=1;					//LED0灭
		 LED1=0;					//LED1亮
		 delay_ms(500);
	 }
}
*/





/*
下面主函数使用直接操作结存器方式实现跑马灯
*/

/*int main(void)
{ 
    HAL_Init();                    	 	//初始化HAL库    
    Stm32_Clock_Init(RCC_PLL_MUL9);   	//设置时钟,72M
	delay_init(72);               		//初始化延时函数
	LED_Init();							//初始化LED	

	while(1)
	{
      GPIOB->BSRR=GPIO_PIN_5;     	//LED0亮
	  GPIOE->BSRR=GPIO_PIN_5<<16; 	//LED1灭
	  delay_ms(500);
      GPIOB->BSRR=GPIO_PIN_5<<16; 	//LED0灭
	  GPIOE->BSRR=GPIO_PIN_5;     	//LED1亮
	  delay_ms(500);
	 }
}*/	
