# 北京水费信息Home Assistant集成
# BeiJing Water Meter Statictics

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Stable](https://img.shields.io/github/v/release/WeiYang1982/bj_water)](https://github.com/WeiYang1982/bj_water/releases/latest)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

# v3更新
1. 登录方式改为**手机号 + 短信验证码**，与官方 App 的登录流程一致：不再需要抓包填写 token、secretKey、md5Salt，在界面上输入手机号、收到并输入验证码即可
2. 手机号按 App 的方式加密后请求验证码，登录成功后自动读取账号**绑定的水表列表**（`getBindAccountList`），并列出所有户号让你选择：
   - 列表里只有一个户号时也会显示「选择水表户号」确认页，不会跳过
   - 多个户号时用下拉框选择，选中哪个就为哪个创建集成条目（要加其他户号，再次添加集成即可）
   - 绑定列表为空时回退到「默认户号」接口，取到了同样让你确认；全都取不到才会提示手动填写户号
3. 登录态（token）过期或失效时，集成会提示重新登录，重新走一遍手机号 + 验证码流程即可，无需删除重建
4. token 有效期改为直接解析 JWT 中的过期时间；拿不到时按 104 天估算，有效期 sensor 会显示剩余天数

# v2更新
1. 由于接口变动，需要抓包获取token、secretKey、md5Salt三个参数，重新设计config flow，页面上增加这三个参数，并增加加解密逻辑
2. 由于token会过期，尚未找到刷新token的相关接口，所以增加了有效期相关sensor，104天过期


## 功能
 - 通过水表户号获取水费账单及历史用水量(仅限北京市)
 - 支持多个水表户号
 - 全程通过UI配置

## 接入数据
 - 水表总数
 - 单价
   - 水费单价(第一阶梯、第二阶梯)
   - 水资源费单价
   - 污水处理费单价
   - 当前阶梯总单价 即几个单价的总和
 - 使用量
   - 第一阶梯总用量
   - 第一阶梯剩余
   - 第二阶梯剩余
 - 当前年份周期费用
   - 缴费状态
   - 缴费日期
   - 当期总水费
   - 当期水资源费
   - 当前污水处理费
   - 当期水费
 - 当前年份周期使用量
   - 当期总用水量
   - 当期水表数

## 使用方法

### 安装

使用[HACS](https://hacs.xyz/)或[手动下载安装](https://github.com/WeiYang1982/bj_water/releases)

**注意：本集成需求`Home Assistant`最低版本为`2026.8`**

### 配置
1. 在「设置 - 设备与服务 - 添加集成」中搜索「北京水费」
2. 输入注册「北京自来水」App 使用的手机号，点击提交，会向该手机号发送短信验证码
3. 输入收到的短信验证码即可完成登录，户号输入框留空即可：
   - 登录成功后会出现「选择水表户号」页，列出账号下所有户号（多个时带地址），选择后才会创建实体
   - 也可以跳过选择直接填写户号（必须是当前账号已绑定的水表，否则提示 `该户号不在当前账号的绑定列表中`）
4. 配置阶段的校验只调用**账号级接口**（用户信息 `getUserInfo`、绑定水表列表 `getBindAccountList`），
   不会拉账单/用水数据；账单数据在集成加载完成后由后台按天刷新
5. 登录态失效后 HA 会提示「重新认证」，按提示重新输入手机号 + 验证码即可

### 更新数据
固定设置为1天更新一次
```
UPDATE_INTERVAL = timedelta(days=1)
```

### 传感器
 - **水表总数**  sensor.xxx_meter_value
 - **当前水费单价**   sensor.xxx_total_cost
 - **水资源费**   sensor.xxx_water_tax
 - **污水处理费**   sensor.xxx_wastwater_treatment_price
 - **第一阶梯水费**  sensor.xxx_first_step_price
 - **第一阶梯总用量**  sensor.xxx_total_usage
 - **第一阶梯剩余**   sensor.xxx_first_step_left
 - **第二阶梯剩余**  sensor.xxx_second_step_left
 - **历史账单周期费用**   sensor.xxx_2023_02_01_fee
     - 额外属性: 缴费状态、缴费日期、总水费、水资源费、污水处理费、水费
 - **历史账单周期用量**   sensor.xxx_2023_02_01_usage
     - 额外属性: 总用水量、水表数


*sensors列表:*

<img alt="sensors列表" height="600" src="https://github.com/WeiYang1982/bj_water/blob/main/images/sensors.png" width="600"/>


*历史账单周期费用*

<img alt="sensors列表" height="600" src="https://github.com/WeiYang1982/bj_water/blob/main/images/history_fee.png" width="600"/>


*历史账单周期用量*

<img alt="sensors列表" height="600" src="https://github.com/WeiYang1982/bj_water/blob/main/images/history_usage.png" width="600"/>


## TODO
1. 由于未使用到第二阶梯，顾暂不处理
2. 数据更新频率固定，且未考虑历史数据是否需要更新(不会...)


- - -
自定义集成教程参考：[Building a Home Assistant Custom Component Part 1: Project Structure and Basics](https://aarongodfrey.dev/home%20automation/building_a_home_assistant_custom_component_part_1/)
