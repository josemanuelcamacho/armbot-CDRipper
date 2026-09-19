#ifndef _HW_ACTION_CTL_
#define _HW_ACTION_CTL_
#include "actions.h"



class HW_ACTION_CTL{
  public:
    uint8_t extended_func_angles[5] = { 90,90,90,90,90 }; /* 二次开发例程使用的角度数值(angle values used for secondary development) */
    //控制执行动作组(control to execute the action group)
    void action_set(int num);
    int action_state_get(void);
    void action_task(void);
    
  private:
    //动作组控制变量(action group control variable)
    int action_num = 0;
};

#endif //_HW_ACTION_CTL_
