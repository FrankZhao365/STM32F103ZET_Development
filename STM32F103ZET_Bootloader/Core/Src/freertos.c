/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * File Name          : freertos.c
  * Description        : Code for freertos applications
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */

/* Includes ------------------------------------------------------------------*/
#include "FreeRTOS.h"
#include "task.h"
#include "main.h"
#include "cmsis_os.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include "usart.h"
#include "uart_user.h"
#include "isotp.h"
#include "isotp_defines.h"
#include "isotp_user.h"
#include "isotp_port.h"
#include "Updater.h"
#include "LinMaster.h"
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define UART_Transmit_Cycle_MAX 1000
/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
/* USER CODE BEGIN Variables */
  /* ISO-TP 报文的大数组和接收回调已经挪到 ASW 层：
     Software/ASW/Updater/src/Updater.c  */

  uint16_t UART_Transmit_Cycle = 0;
  uint8_t UART_ARRY[8] = {0xAA,0xAA,0xAA,0xAA,0xAA,0xAA,0xAA,0xAA};
  uint8_t LIN_Timer = 0;
/* USER CODE END Variables */
osThreadId defaultTaskHandle;
osThreadId BSW_10ms_TaskHandle;

/* Private function prototypes -----------------------------------------------*/
/* USER CODE BEGIN FunctionPrototypes */

/* USER CODE END FunctionPrototypes */

void StartDefaultTask(void const * argument);
void Start_BSW_10ms_Task(void const * argument);

void MX_FREERTOS_Init(void); /* (MISRA C 2004 rule 8.1) */

/* GetIdleTaskMemory prototype (linked to static allocation support) */
void vApplicationGetIdleTaskMemory( StaticTask_t **ppxIdleTaskTCBBuffer, StackType_t **ppxIdleTaskStackBuffer, uint32_t *pulIdleTaskStackSize );

/* USER CODE BEGIN GET_IDLE_TASK_MEMORY */
static StaticTask_t xIdleTaskTCBBuffer;
static StackType_t xIdleStack[configMINIMAL_STACK_SIZE];

void vApplicationGetIdleTaskMemory( StaticTask_t **ppxIdleTaskTCBBuffer, StackType_t **ppxIdleTaskStackBuffer, uint32_t *pulIdleTaskStackSize )
{
  *ppxIdleTaskTCBBuffer = &xIdleTaskTCBBuffer;
  *ppxIdleTaskStackBuffer = &xIdleStack[0];
  *pulIdleTaskStackSize = configMINIMAL_STACK_SIZE;
  /* place for user code */
}
/* USER CODE END GET_IDLE_TASK_MEMORY */

/**
  * @brief  FreeRTOS initialization
  * @param  None
  * @retval None
  */
void MX_FREERTOS_Init(void) {
  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* USER CODE BEGIN RTOS_MUTEX */
  /* add mutexes, ... */
  /* USER CODE END RTOS_MUTEX */

  /* USER CODE BEGIN RTOS_SEMAPHORES */
  /* add semaphores, ... */
  /* USER CODE END RTOS_SEMAPHORES */

  /* USER CODE BEGIN RTOS_TIMERS */
  /* start timers, add new ones, ... */
  /* USER CODE END RTOS_TIMERS */

  /* USER CODE BEGIN RTOS_QUEUES */
  /* add queues, ... */
  /* USER CODE END RTOS_QUEUES */

  /* Create the thread(s) */
  /* definition and creation of defaultTask */
  osThreadDef(defaultTask, StartDefaultTask, osPriorityNormal, 0, 128);
  defaultTaskHandle = osThreadCreate(osThread(defaultTask), NULL);

  /* definition and creation of BSW_10ms_Task */
  osThreadDef(BSW_10ms_Task, Start_BSW_10ms_Task, osPriorityIdle, 0, 512);
  BSW_10ms_TaskHandle = osThreadCreate(osThread(BSW_10ms_Task), NULL);

  /* USER CODE BEGIN RTOS_THREADS */
  /* add threads, ... */
  /* USER CODE END RTOS_THREADS */

}

/* USER CODE BEGIN Header_StartDefaultTask */
/**
  * @brief  Function implementing the defaultTask thread.
  * @param  argument: Not used
  * @retval None
  */
/* USER CODE END Header_StartDefaultTask */
void StartDefaultTask(void const * argument)
{
  /* USER CODE BEGIN StartDefaultTask */
  /* Infinite loop */
  for(;;)
  {
    osDelay(1);
  }
  /* USER CODE END StartDefaultTask */
}

/* USER CODE BEGIN Header_Start_BSW_10ms_Task */
/**
* @brief Function implementing the BSW_10ms_Task thread.
* @param argument: Not used
* @retval None
*/
/* USER CODE END Header_Start_BSW_10ms_Task */
void Start_BSW_10ms_Task(void const * argument)
{
  /* USER CODE BEGIN Start_BSW_10ms_Task */
  UART_Printf_Init();

  /* 注册 Updater 的接收回调：之后整条报文会在中断里直接拷进 Updater 的大数组 */
  Updater_Init();

  /* Infinite loop */
  for(;;)
  {
    osDelay(1);

    /* ISO-TP 的 Cr(等待流控帧)超时定时器必须由任务周期性驱动，
       否则对端发来首帧后收不到流控帧，会一直等到超时才丢弃。 */
    IsotpPort_Poll();

    /* 把 ISO-TP 库在中断里攒下的诊断信息真正发出去 */
    IsotpUser_DebugPump();

    /* 驱动 Updater：取走新到的完整报文并交给 Updater_OnFrame() 处理 */
    Updater_Poll();

    /* 固件接收完成(RequestTransferExit 校验通过后置位)：
       把 RAM 里攒下的 CAN_RX_BUF 整个写入内部 Flash 的 APP 区。
       写完后该函数会复位 MCU，由 bootloader 重新校验并跳转。 */
    if(Updater_IsFirmwareReady() == 1U)
    {
      (void)Updater_FlushFirmware();
    }

    if(UART_Transmit_Cycle < UART_Transmit_Cycle_MAX)
    {
      UART_Transmit_Cycle++;
    }
    else
    {
      UART_Transmit_Cycle = 0;

      /* LED0 反转：与发送同步，指示本任务存活 */
      HAL_GPIO_TogglePin(LED0_GPIO_Port, LED0_Pin);
      LIN_Message_Send();
      printf("\r\nSTM32 HeartBeat\r\n");

      /* 走 ISO-TP 发送(而不是裸 HAL_CAN_AddTxMessage)，
         报文超过 8 字节时由库自动拆成 FF + CF 多帧。 */
      // IsotpPort_Send(UART_ARRY, sizeof(UART_ARRY));
    }
  }
  /* USER CODE END Start_BSW_10ms_Task */
}

/* Private application code --------------------------------------------------*/
/* USER CODE BEGIN Application */

/* USER CODE END Application */
