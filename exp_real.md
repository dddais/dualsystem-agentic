# 真机实验功能需求

vla 策略执行动作，监控器监控动作执行，发现目标错误后终止当前动作，reset机器人关节角，然后再执行。

## vla执行模块

robot-bride 分布式框架

## 分层框架

### 监控模块

输入：三视角图像
处理：sam3 获取主视角bbox，通过输入GRM和GRM-attention得到输出，通过margin判断目标错误
输出：错误信号

### 状态机

ready->执行->终止恢复->ready
包含三个状态：ready->执行->中止与恢复 ：

- ready状态下等待键盘输入，然后发送执行开始给robot_runtime，同时启动monitor；然后进入执行状态；
- 执行状态下按一定频率查询得到monitor状态，如果不是progress则进入中止与恢复阶段
- 进入中止与恢复阶段后，调用中止与恢复的tool发送给robot_runtime
- 然后进入ready阶段，这样循环

### 接入真机vla

在现有vla部署系统基础上，尽量少修改，完成分层框架的mcp实现，主要需要关注下面的功能实现

- 开始推理
- 切换vla instruction
- 停止执行
- 恢复状态

### 最简化版本

- 服务器：GRM部署，推理
- 从臂端：robot runtime 
  - 获取图像，通过http传出：待实现
  - vla相关操作如开始，停止，恢复通过手动操作
- 从臂端：执行loop



### 简化版本

- 服务器：GRM部署，推理
- 从臂端：robot runtime
  - 获取图像，通过http传出：待实现
  - vla相关操作接入真实的robot-bridge
    - 输入指令，开始推理执行
    - 停止执行
    - 恢复原状
    - 参考已有ui端的控制，能不能类似于那个发送指令
- 从臂端：执行loop

