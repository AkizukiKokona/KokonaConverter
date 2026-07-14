# pmx2fbx

> 一键式 PMX → FBX 转换工具，专门面向 **Unreal Engine 4.27** 的骨骼网格体导入。
> 零第三方依赖，纯标准库实现，Windows 上双击即用。

把 MikuMikuDancing 的 `.pmx` 模型尽可能完整地搬进 UE4.27：骨骼层级、蒙皮权重、材质、贴图、顶点变形（Morph）全部保留，并在坐标/UV/缩放上做 UE 友好的转换。

---

## 特性

### 1. 解析与导出
- **PMX 2.0 / 2.1** 二进制格式完整解析（手写解析器，零依赖）
- 顶点、面、材质、骨骼、变形、显示帧、刚体、关节、软体全部读出
- **FBX 7.4.0 二进制（Binary）** 输出（`FBXVersion: 7400`），UE4.27 官方推荐版本
- **贴图嵌入**：贴图数据直接写入 FBX 二进制（`Video.Content`），UE4 导入时无需外部贴图文件

### 2. 保留的内容
| 类别 | 说明 |
|---|---|
| 骨骼层级 | 完整保留父子关系、局部平移/旋转、固定轴、局部坐标系、继承旋转/平移、外部亲；多根骨骼时自动注入合成 `Root` 骨保证 UE4 单根骨骼 |
| 蒙皮权重 | BDEF1 / BDEF2 / BDEF4 完整保留；SDEF 退化为 BDEF2（线性）；QDEF 退化为 BDEF4 |
| 每顶点最大骨骼数 | 默认 4（UE4 默认值），多余权重自动合并、归一化、裁剪 |
| 顶点变形 (Vertex Morph) | 转为 FBX Blend Shape，UE4 中即 Morph Target |
| 材质 | 漫反射 / 镜面 / 环境光 / 自发光颜色、不透明度、Toon、边缘、双面、地面阴影开关 |
| 贴图 | 漫反射贴图、Sphere 贴图、Toon 贴图（共享与索引两种） |
| 多 UV | PMX 的附加 UV1~UV4 作为额外 UV 通道写入 |
| BindPose | 写入 `BindPose` 节点，确保 UE4 蒙皮绑定正确 |

### 3. 坐标/单位/UV 转换（针对 UE4.27）
| 项目 | PMX | UE4.27 | 处理 |
|---|---|---|---|
| 上方向 | Y | Z | 循环置换 `(x,y,z) → (z,x,y)` |
| 前方向 | Z | X | 同上 |
| 单位 | 1 单位 = 8 cm（默认） | 1 单位 = 1 cm | 乘以 `scale`（默认 8.0，可调） |
| UV 原点 | 左上角 | 左下角 | V 翻转 `v → 1 - v` |
| 三角形绕序 | 顺时针 (CW) | 逆时针 (CCW) | 顶点绕序翻转 |
| 骨骼四元数 | 同 PMX 坐标系 | 同 UE 坐标系 | 循环置换四元数分量 |

### 4. 贴图与中文路径
- 支持中文路径（文件名、目录名均可用中文）
- 贴图查找顺序（按优先级）：
  1. PMX 中记录的原始相对路径
  2. PMX 文件所在目录下的同名文件
  3. PMX 同级 `tex/` 子目录（MMD 常见结构）
  4. PMX 同级任意一级子目录下的同名文件
- 大小写不敏感回退（应对 Windows 之外的拷贝粘贴造成的 case 漂移）
- 共享 Toon 贴图（`toon01.bmp ~ toon10.bmp`）按内置表识别
- **嵌入模式（默认开启）**：贴图文件原始字节直接写入 FBX 二进制的 `Video.Content` 节点，UE4 导入时自动解出贴图，无需额外携带 `textures/` 文件夹
- 可选复制模式：同时把贴图复制到 FBX 同级 `textures/` 子目录（用于非嵌入工作流或备份）

### 5. 无法迁入 FBX 的数据
FBX 格式本身不支持以下 PMX 数据，工具会在 FBX 旁边输出一个 `<name>.pmx_meta.json`，把这些数据完整记录下来供查阅：
- **IK 链**（目标骨、循环次数、角度限制、IK 链接）—— UE4 有自己的 IK 系统
- **刚体 / 关节**（Bullet 物理）—— UE4 用自己的物理引擎
- **显示帧**（Display Frame）—— 仅记录结构
- **非顶点变形**：Group / Bone / UV / UV1~4 / Material / Flip / Impulse 变形——FBX 无对应概念
- **SDEF 参数**（C / R0 / R1）—— 退化为 BDEF2 线性插值
- **QDEF 双四元数权重** —— 退化为 BDEF4 线性插值

---

## 使用方法

### 方式 1：双击 / 拖拽（推荐）
1. 安装 Python 3.8+（自带 tkinter，Windows 官方安装包勾选 "tcl/tk and IDLE" 即可）
2. 双击 `run.bat` 启动图形界面
3. 点 "浏览" 选择 `.pmx` 文件，自动填好输出路径
4. 勾选 "嵌入贴图到 FBX（推荐）" 以将贴图数据嵌入 FBX 二进制（默认已勾选）
5. 点 "▶ 转换"，等待日志显示完成
6. 点 "打开输出目录" 直接在资源管理器中打开 FBX 所在文件夹

**或者**：把一个或多个 `.pmx` 文件直接拖到 `run.bat` 上，会在命令行依次转换。

### 方式 2：命令行
```bat
:: 基本用法（输出到同目录同名 .fbx）
python main.py "我的模型.pmx"

:: 指定输出路径
python main.py "我的模型.pmx" "D:\out\model.fbx"

:: 自定义缩放（1 PMX 单位 = 10 cm）
python main.py "我的模型.pmx" --scale 10.0

:: 不复制贴图（仅引用原路径）
python main.py "我的模型.pmx" --no-copy-textures

:: 不嵌入贴图（仅复制到 textures/ 子目录引用）
python main.py "我的模型.pmx" --no-embed-textures

:: 不导出变形
python main.py "我的模型.pmx" --no-morphs

:: 不写 BindPose
python main.py "我的模型.pmx" --no-bind-pose
```

完整参数：
```
python main.py [input.pmx] [output.fbx]
               [--scale N]              缩放系数，默认 8.0
               [--no-copy-textures]    不复制贴图到 FBX 旁
               [--no-embed-textures]   不嵌入贴图字节到 FBX 二进制
               [--no-morphs]           跳过顶点变形导出
               [--no-bind-pose]        跳过 BindPose 写入
               [--gui]                 强制启动图形界面
```

---

## 导入 UE4.27

1. 把生成的 `.fbx` 拖进 Content Browser
2. 在导入对话框中：
   - **Skeletal Mesh** ✓（不是 Static Mesh）
   - **Import Mesh** ✓
   - **Skeleton**：第一次导入留空，UE4 会自动创建；后续可指定同一套骨骼以便共用动画
   - **Import Morph Targets** ✓（如果 PMX 里有顶点变形）
   - **Create Physics Asset** ✓（自动生成物理资产占位）
   - **Normal Import Method**：`Compute Normals`（PMX 顶点法线已正确导出，但 UE4 重新计算通常更平滑）
3. 贴图：**默认嵌入模式**下贴图已内嵌于 FBX 二进制，UE4 会自动解出并创建纹理对象，无需额外文件；若使用 `--no-embed-textures` 则需确保 FBX 同级 `textures/` 文件夹存在
4. 材质：FBX 中已用 Phong 模型；UE4 导入后默认转成 `MI_` 实例，可手动改回 `M_` 主材质以接入你的着色器

### 常见导入问题
| 现象 | 原因 / 处理 |
|---|---|
| 模型很大或很小 | 调整 `--scale`；MMD 默认 8 cm/单位，UE4 默认 1 cm/单位 |
| 模型朝向不对 | 工具已做 Y-up→Z-up 转换；若仍偏，可在 UE4 的 Skeleton 编辑器里整体旋转根骨 |
| 贴图丢失 | 默认嵌入模式不会丢贴图；若关闭了嵌入，检查 `textures/` 是否在 FBX 旁边、中文路径是否完整 |
| 蒙皮错乱 | 检查 `.pmx_meta.json` 中的 `weight_types`，SDEF/QDEF 已退化为线性，少量模型会略有形变 |
| Morph 没出现 | 确认导入时勾选了 Import Morph Targets；只有 Vertex Morph（type 1）会迁移 |

---

## 项目结构

```
.
├── pmx_reader.py        # PMX 2.0/2.1 二进制解析器（零依赖）
├── fbx_writer.py        # FBX 7.4.0 二进制写入器 + 贴图嵌入（零依赖）
├── texture_utils.py     # 贴图路径解析 / 中文路径 / tex 子目录识别 / 字节读取
├── convert.py           # 转换编排器 + JSON 元数据 sidecar
├── main.py              # 入口：tkinter GUI + CLI（含"打开输出目录"按钮）
├── run.bat              # Windows 启动器（双击或拖拽）
├── test_pmx_generator.py    # 简单测试模型生成器
├── test_pmx_complex.py      # 复杂测试模型生成器（多权重/IK/固定轴）
├── README.md
└── LICENSE
```

### 运行依赖
- **Python 3.8+**（仅用标准库：`struct`、`os`、`sys`、`json`、`math`、`argparse`、`tkinter`、`threading`、`dataclasses`、`typing`、`zlib`、`array`）
- **无任何 pip 包**，无需 `pip install` 任何东西
- **tkinter**：Windows 官方 Python 安装包默认包含；Linux 如需 GUI 需 `apt install python3-tk`

---

## 设计取舍说明

### 为什么手写解析器？
PMX 格式规范公开但变长索引（1/2/4 字节，有符号/无符号混用）和小端字节序让通用库容易出错。手写解析器可以精确控制每个字段，且零依赖便于分发——用户拷贝整个文件夹就能用，不用担心环境差异。

### 为什么用二进制 FBX 而不是 ASCII？
- **贴图嵌入**：FBX ASCII 格式**不支持**嵌入媒体（Autodesk 官方文档明确说明 "This option is available only when exporting to FBX binary files"）。只有二进制 FBX 才能把贴图字节写入 `Video.Content` 节点，让 UE4 导入时自动解出贴图，无需额外携带 `textures/` 文件夹
- UE4.27 对二进制和 ASCII FBX 支持度相同，但二进制文件更小、解析更快
- 7.4.0 是 UE4.27 自动导入 FBX 时识别最稳定的版本
- 写入器完全可控，不依赖 Autodesk SDK；使用 Python 标准库 `struct`/`zlib`/`array` 手写二进制序列化

### 为什么 SDEF 退化为 BDEF2？
SDEF（Spherical Defromation）的 C/R0/R1 三点参数定义了一个球面插值，能避免 BDEF2 在关节弯曲时的"糖纸"形变。但 FBX 标准的蒙皮只支持线性权重，没有 SDEF 概念。退化后形态会略差，但 UE4 中可通过加骨骼 / Morph Target 补救。完整的 SDEF 参数已记录在 sidecar JSON 中。

### 为什么不迁入刚体/关节？
PMX 的刚体/关节基于 Bullet 物理引擎，与 UE4 的 PhysX/Chaos 物理模型完全不同。即使强行写入 FBX 的物理节点，UE4 也无法识别。建议在 UE4 中重建物理资产。所有刚体/关节参数已记录在 sidecar JSON 中备查。

---

## 限制

- 不处理 PMD 格式（PMD 是 PMX 的前身，可用 PMX Editor 升级后再转换）
- 不处理软体（Soft Body）—— PMX 软体极为罕见，且 FBX 无对应概念
- 骨骼变形（Bone Morph）、UV 变形、材质变形、组变形、Flip 变形、Impulse 变形不迁入 FBX（已记录到 sidecar）
- 不做动画（VMD）转换——本工具只处理静态模型

---

## 许可证

见 [LICENSE](LICENSE)。

---

## 致谢

- PMX 格式规范：[MMD 规范文档](https://gist.github.com/Dispenser/bd4c5a2cf1f1f1d5c7f6f5d2c5d5e5e5)（社区整理版）
- FBX 二进制格式：参考 Autodesk FBX SDK 文档、[Kaitai Struct FBX 格式描述](https://github.com/kaitai-io/fbx_format) 及开源项目 `fbx2json` / `json2fbx` 的格式样例
- UE4.27 导入规范：参考 Epic Games 官方 [FBX Skeletal Mesh Pipeline](https://dev.epicgames.com/documentation/en-us/unreal-engine/fbx-skeletal-mesh-pipeline-in-unreal-engine)
