# robot-jaka-lumi_with_tg9801_gripper

JAKA Lumi（12 自由度移动操作机器人 + TG-9801 夹爪）的 Robonix 部署仓库。

```text
robot-jaka-lumi_with_tg9801_gripper/
├── robonix_manifest.yaml          提交版：全部按 url 引用
├── robonix_manifest.local.yaml    本机联调版：path 引用兄弟目录（gitignore）
├── soma.yaml                      本体描述、footprint、逻辑部件树
├── urdf/jaka_lumi.urdf            整机坐标树（27 link / 26 joint，10 个可动）
├── urdf/meshes/*.STL              26 个网格，17 MB
├── config/rtabmap_params.yaml     RGB-D 建图参数
├── config/nav2_params.yaml        Nav2 参数（无 scan 层的取舍写在文件头）
├── tools/prepare_urdf.py          从厂商模型生成上面那份 URDF（可重跑）
├── tools/check_deployment.py      静态自检：清单/soma/URDF/参数/环境变量
├── build.sh / start.sh            构建与启动入口
├── .vlmkey.example                VLM 凭据 + 头部相机序列号模板
└── README.md
```

## 部署实例

| 实例 | 来源 | 职责 |
|---|---|---|
| `lumi_chassis` | **新建** `primitive-jaka-lumi-chassis-rbnx` | 底盘：odom + move + `/cmd_vel`，厂商 TCP JSON :31001 |
| `lumi_body` | **新建** `primitive-jaka-lumi-body-rbnx` | 升降/腰/头 4 关节 → `/joint_states`，厂商 HTTP :5000 |
| `jaka_arm` | **复用** `primitive-jaka-mini_with_tg9801_gripper-arm_gripper-rbnx` | 6 轴臂 + TG-9801（同款 Mini 2、同款夹爪） |
| `head_camera` | **复用** `primitive-orbbec-camera-rbnx` | 头部 Gemini 2L（`gemini2L.launch.py`） |
| `robot_description` | **复用** `primitive-robot-description-rbnx` | 从 Soma 取 URDF，跑 robot_state_publisher 出 TF |
| `mapping` | **复用** `service-map-rbnx` | RTAB-Map RGB-D 建图，输出占据栅格与 `map→odom` |
| `navigation` | **复用** `service-navigation-rbnx` | Nav2，绑 map + odom，输出 `/cmd_vel` |

## 硬件地址一览

| 部件 | 接口 |
|---|---|
| 机械臂（JAKA Mini 2） | jkrc SDK → `192.168.10.90`（TCP） |
| 升降/腰/头（4 外部轴） | HTTP → `http://192.168.10.90:5000` |
| 底盘 | TCP JSON → `192.168.10.10:31001`（网页 `http://192.168.10.10:9001`） |
| 头部 RGB-D（Orbbec Gemini 2L） | USB 3.0（底盘面板口 1） |
| 躯干 RGB-D（同型号） | 同一 USB 口，V1 不驱动 |

> ⚠️ WiFi 直连时 DHCP 给的 `10.5.5.x` 是 AP 自己的池。必须给该连接加一个
> `192.168.10.x/24` 静态地址，否则 `192.168.10.90/10` 两个地址都不通：
> `nmcli connection modify Lumi10000023 +ipv4.addresses 192.168.10.200/24`

## 前置条件

1. **Robonix**：`make install` + `rbnx setup ~/robonix`。
2. **ROS 2 包**（本机当前缺前两个，需要装）：

   ```bash
   sudo apt install ros-humble-rtabmap-ros ros-humble-nav2-bringup \
                    ros-humble-robot-state-publisher ros-humble-rmw-zenoh-cpp
   ```

3. **JAKA SDK 二进制**：臂包不随仓库分发 `jkrc.so` / `libjakaAPI.so`，需放在
   `primitive-jaka-mini_with_tg9801_gripper-arm_gripper-rbnx/jaka_arm/jaka_sdk/aarch64-linux-gnu/`
   （本机已就绪）。缺失时臂包的 `build.sh` 会 fail-fast 并提示路径。
4. **`.vlmkey`**：`cp .vlmkey.example .vlmkey`，填 VLM 凭据与**头部相机序列号**
   （两台同型号相机必须按序列号绑定，否则可能把躯干相机当成建图相机）。
5. **本地清单是全离线的**：`robonix_manifest.local.yaml` 把 7 个实例全部改成
   `path:` 指向 `~/` 下的兄弟目录——`primitive-jaka-lumi-chassis-rbnx`、
   `primitive-jaka-lumi-body-rbnx`、
   `primitive-jaka-mini_with_tg9801_gripper-arm_gripper-rbnx`、
   `primitive-orbbec-camera-rbnx`、`primitive-robot-description-rbnx`、
   `service-map-rbnx`、`service-navigation-rbnx`。
   提交版清单仍是 `url:`；没有这些兄弟目录的机器要么逐个 clone，要么用提交版
   （首次构建需要网络）。

## 构建

```bash
python3 tools/check_deployment.py     # 静态自检，应输出 5 passed, 0 failed
bash build.sh                         # 建部署 venv + rbnx build -f（逐包编译，较慢）
```

`tools/prepare_urdf.py` 只在更换模型或调整坐标系时需要重跑：

```bash
LUMI_MODEL="$HOME/jaka-lumi/02. JAKA Lumi Product Manual [产品手册]/JAKA-Lumi-sensors-v3"
python3 tools/prepare_urdf.py \
    --source "$LUMI_MODEL/urdf/JAKA-Lumi-sensors-v3.urdf" \
    --vendor-meshes "$LUMI_MODEL/meshes"
```

它做三件事并自检：臂关节 `l_a1..a6` → `joint_1..joint_6`（对齐臂包的硬编码
关节名）、`package://` 网格路径改相对路径、10 个无反馈的被动关节（轮子/脚轮）
改 `fixed` 走 `/tf_static`。另外把 CAD 底盘包在新建的 `base_link` 之下并旋转
90°，因为厂商模型的轮轴在 X 上、前向传感器在 -Y 侧，**模型前向是 -Y**，不符合
ROS 的 REP-103（X 前、Y 左）。这个旋转常量在脚本顶部 `BASE_YAW_DEG`。

## 启动与验收

四个阶段，逐级放开。**第一次上电绝不能直接开 `motion_enabled`。**

### 阶段 0：静态（无需上电）

```bash
python3 tools/check_deployment.py
rbnx validate ../primitive-jaka-lumi-chassis-rbnx
rbnx validate ../primitive-jaka-lumi-body-rbnx
bash build.sh
```

通过标准：自检 0 failed、两个 `rbnx validate` 通过、`build.sh` 退出码 0。

### 阶段 1：只读（机器人已上电、急停释放、三个 `motion_enabled` 均为 false）

```bash
bash start.sh
```

另一个终端：

```bash
rbnx caps -v | rg -A6 'jaka_arm|lumi_body|lumi_chassis|head_camera|mapping|navigation'
rbnx logs -d ./rbnx-boot/logs -l warn
ros2 run tf2_ros tf2_echo base_link Camera3_Link   # 相机进 TF 树
ros2 run tf2_ros tf2_echo odom base_link           # 底盘里程计
ros2 run tf2_ros tf2_echo map base_link            # mapping 起来之后
```

通过标准：6 个 provider 全部 `ACTIVE`；`/joint_states` ≈10 Hz 且含 `l_1..l_4`；
三条 TF 都能解析；无 driver/首帧错误。

### 阶段 2：无运动功能

```bash
rbnx ask '只调用 provider_id=head_camera 的 robonix/primitive/camera/snapshot 取一帧，不要执行任何运动。'
```

### 阶段 3：逐级放开运动

1. `lumi_body.motion_enabled: true` → 升降 +10 mm、腰部 +5°、头部 ±5°。
   **同时看 RViz：机器人模型的前向应与 +X 一致**；如果反了，把
   `tools/prepare_urdf.py` 里的 `BASE_YAW_DEG` 改成 `-90` 重新生成。
2. `lumi_chassis.motion_enabled: true` → `forward_m: 0.1`、`rotate_deg: 10`，
   确认里程闭环、停车与方向。
3. `jaka_arm.motion_enabled: true` → 小角度关节运动 + 夹爪开合。
   **换夹爪后必须先设 tool data 与 payload**，否则 TCP 与碰撞检测都是错的。
4. 最后才允许 `rbnx ask` 走完整流程。

### 阶段 4：关闭

```bash
rbnx shutdown -f ./robonix_manifest.local.yaml
```

通过标准：`on_shutdown` 被调用、底盘补发停车、`rbnx-boot/state.json` 移除、
无残留子进程。

## 已知限制

| # | 限制 | 影响 / 后续 |
|---|---|---|
| 1 | 240° 激光数据不可得 | Nav2 的 costmap 只有静态层 + 膨胀层，看不见新出现的障碍；底盘固件仍会碰撞停车。V2 可做 depth→pointcloud→scan |
| 2 | 臂 6 关节不进 TF | 3D 抓取拿不到末端位姿；jaka 抓取 skill 用 2D 单应 + 臂基座系，不受影响。V1.1 可给 `lumi_body` 加 `publish_arm_joints`（第二个 jkrc 连接）或 fork 臂包 |
| 3 | 躯干相机未驱动 | 抓取缺少"操作引导"视角；两台相机共用一个 USB 3.0 口，同时开 1280×800 会抢带宽 |
| 4 | 两套地图坐标系 | 厂商导航（`chassis/move` → `/api/move`）与 Robonix 的 map 不同源；混用会跑偏。可用 `lumi_chassis.vendor_pose_offset` 标定对齐 |
| 5 | 底盘无轮式编码器 | `/odom` 是厂商 SLAM 位姿，`twist` 由位姿差分得出，精度不如轮速 |
| 6 | 语音模块未接入 | AIUI XF-USB-MC 是标准 USB 声卡，后续可复用 `primitive-audio-driver-rbnx` |

## 排障：三个踩过的坑

### 1. 仓库改名后 build 失败（`CMakeCache.txt ... is different than`）

`rbnx-build/` 和 `.venv/` 都会把**绝对路径**写进构建产物：CMake 缓存在
`rbnx-build/codegen/ros2_idl/build/*/CMakeCache.txt` 里记着源码目录，`pip` 可执行
文件的 shebang 指向 venv 解释器。仓库一旦改名或搬动，这些缓存全部失效，症状是：

```
CMake Error: The current CMakeCache.txt directory ... is different than the directory
             /home/nvidia/<老仓库名>/rbnx-build/... where CMakeCache.txt was created
CMake Error: The source directory "/home/nvidia/<老仓库名>/..." does not exist
```

或者更隐蔽的：

```
.venv/bin/pip: /home/nvidia/<老仓库名>/.venv/bin/python3: bad interpreter: No such file or directory
```

**处理**：删掉该包的 `rbnx-build/` 和 `.venv/` 再构建（两者都是可再生的，
已在各包的 `.gitignore` 里）。批量排查：

```bash
for d in ~/primitive-*/rbnx-build/codegen/ros2_idl/build/*/CMakeCache.txt; do
  stale=$(grep -m1 '^CMAKE_HOME_DIRECTORY:INTERNAL=' "$d" | cut -d= -f2)
  [ -n "$stale" ] && [ ! -d "$stale" ] && echo "STALE $(echo $d | sed 's|/rbnx-build/.*||') -> $stale"
done | sort -u
```

### 2. `rbnx build -f` 不会因为单个包失败而停下

日志里会继续打印后面的包，只有末尾的 **Build summary** 才列出 `Failed`。看中间
日志时留意 `✓ Package '...' build finished` 这一行是否出现，别只看最后一段。

### 3. 首次构建需要能访问 GitHub

提交版清单里的 `robot_description` 是 `url:`，`rbnx build` 会 clone 到
`rbnx-boot/cache/`。代理不稳时会出现 `gnutls_handshake() failed`。离线机器用
`robonix_manifest.local.yaml`（全 `path:`），并确保七个兄弟目录都存在。

## 许可

Apache-2.0。
