#include "LinMaster.h"
#include "usart.h"
#include <stdint.h>


uint8_t LIN_Tx_Data[20];

uint8_t pid_Calc (uint8_t ID)
{
    if(ID>0x03F)
    {
        Error_Handler();
    }
    uint8_t IDBuf[6];
    for(int i = 0; i < 6; i++)
    {
        IDBuf[i] = (ID>>i)&0x01;
    }
    uint8_t P0 = (IDBuf[0]^IDBuf[1]^IDBuf[2]^IDBuf[4])&0x01;
    uint8_t P1 = ~((IDBuf[1]^IDBuf[3]^IDBuf[4]^IDBuf[5])&0x01);
    ID = ID | (P0<<6) | (P1<<7);
    return ID;
}

uint8_t Checksum_Calc (uint8_t PID, uint8_t *data, uint8_t size)
{
    uint8_t buffer[size+2];
    uint16_t sum = 0;
    buffer[0] = PID;
    for(int i = 0; i < size; i++)//Data filed
    {
        buffer[i+1] = data[i];
    }   

    for(int i = 0; i < size+1; i++)//Data filed
    {
        sum = sum + buffer[i];
        if(sum>0xff)
        {
            sum = sum-0xff;
        }
    } 
    sum = 0xff - sum;
    return sum;

}

void LIN_Frame_ORG(void)
{
    LIN_Tx_Data[0] = 0x55;//Sync filed
    LIN_Tx_Data[1] = pid_Calc(0x34);
    for(int i = 0; i < 8; i++)//Data filed
    {
        LIN_Tx_Data[i+2] = i;
    }
    LIN_Tx_Data[10] = Checksum_Calc(LIN_Tx_Data[1],LIN_Tx_Data+2,8);
}

void LIN_Message_Send(void)
{
    LIN_Frame_ORG();
    HAL_LIN_SendBreak(&huart2);
    HAL_UART_Transmit(&huart2,LIN_Tx_Data,11,1000);
}
