#include <Servo.h> //导入舵机库(import servo library)
#include <EEPROM.h>

#define EEPROM_START_FLAG "HIWONDER"
#define EEPROM_ACTION_NUM_ADDR 16u   /* 存放动作组内的动作个数(the number of actions in the action group to be saved) */
#define EEPROM_ACTION_START_ADDR 32u /* 动作组的起始地址(the start address of the action group) */
#define EEPROM_ACTION_UNIT_LENGTH 6u /* 动作组的单个动作字节长度(the byte length of a single action in the action group) */

#define EEPROM_SERVO_OFFSET_START_ADDR 0u
#define EEPROM_SERVO_OFFSET_DATA_ADDR 16u
#define EEPROM_SERVO_OFFSET_LEN 6u

/* 引脚定义(define pins) */
const static uint8_t servoPins[5] = { 7, 6, 5, 4, 3};//舵机引脚定义(define servo pins)

static uint8_t eeprom_read_buf[16];

//舵机控制对象(servo control object)
Servo servos[5];

const uint8_t limt_angles[5][2] = {{0,82},{0,180},{0,180},{25,180},{0,180}}; /* 各个关节角度的限制(angle limit for each joint) */

static int8_t servo_offset[6] = { 0 , 0 , 0 , 0 , 0 , 0 };
static uint8_t servo_expect[6];
static uint8_t servo_angles[5] = { 90,90,90,90,90};  /* 舵机实际转动角度数值(the actual rotation angle values of the servos) */
static uint8_t extended_func_angles[5] = { 80,30,140,110,90}; //动作数据(action data)
static void servo_control(void); /* 舵机控制(servo control) */
void user_task(void);

void setup() {
  // put your setup code here, to run once:
  Serial.begin(115200);
  // 设置串行端口读取数据的超时时间(set the timeout for serial port reading data)
  Serial.setTimeout(500);
  
  // 绑定舵机IO口(bind servo IO pin)
  for (int i = 0; i < 5; ++i) {
    servos[i].attach(servoPins[i]);
  }

  //调用偏差读取(call deviation read)
  read_servo_offset();

  delay(2000);
  Serial.println("start");
}

void loop() {
  // 用户任务(user task)
  user_task();

  //舵机控制(servo control)
  servo_control();
}

void user_task(void)
{
  static uint32_t last_tick = 0;
  if (millis() - last_tick < 100) {
    return;
  }
  last_tick = millis();

  Serial.println("The action is running successfully!");
}

// 舵机控制任务（不需修改）(servo control task (no need to modify))
void servo_control(void) {
  static uint32_t last_tick = 0;
  if (millis() - last_tick < 20) {
    return;
  }
  last_tick = millis();

  for (int i = 0; i < 5; ++i) {
    servo_expect[i] = extended_func_angles[i] + servo_offset[i];
    if(servo_angles[i] > servo_expect[i])
    {
      servo_angles[i] = servo_angles[i] * 0.9 + servo_expect[i] * 0.1;
      if(servo_angles[i] < servo_expect[i])
        servo_angles[i] = servo_expect[i];
    }else if(servo_angles[i] < servo_expect[i])
    {
      servo_angles[i] = servo_angles[i] * 0.9 + (servo_expect[i] * 0.1 + 1);
      if(servo_angles[i] > servo_expect[i])
        servo_angles[i] = servo_expect[i];
    }

    servo_angles[i] = servo_angles[i] < limt_angles[i][0] ? limt_angles[i][0] : servo_angles[i];
    servo_angles[i] = servo_angles[i] > limt_angles[i][1] ? limt_angles[i][1] : servo_angles[i];
    servos[i].write(i == 0 || i == 5 ? 180 - servo_angles[i] : servo_angles[i]);
  }
}

void read_servo_offset(void){
   // 读取偏差角度(read deviation angle)
  for (int i = 0; i < 16; ++i) {
    eeprom_read_buf[i] = EEPROM.read(EEPROM_SERVO_OFFSET_START_ADDR + i);
  }
  if (strcmp(eeprom_read_buf, EEPROM_START_FLAG) == 0) {
    memset(eeprom_read_buf,  0 , sizeof(eeprom_read_buf));
    Serial.println("read offset");
    for (int i = 0; i < 6; ++i) {
      eeprom_read_buf[i] = EEPROM.read(EEPROM_SERVO_OFFSET_DATA_ADDR + i);
    }
    memcpy(servo_offset , eeprom_read_buf , 6);
    for (int i = 0; i < 6; ++i) {
      Serial.print(servo_offset[i]);
      Serial.print(" ");
    }
    Serial.println("");
  }


}
