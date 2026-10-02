# Spout

**把一堆静态立绘，变成一只能在 DSH 界面里呼吸的宠物。**
*Turn a folder of static character poses into a living [dsh-pet](https://github.com/zhu1090093659/dsh-web).*

`spout` 是给 [`@linxin666/dsh-pet`](https://github.com/zhu1090093659/dsh-web) 用的宠物孵化器。它吃进一个装着「每种心情一张图」的文件夹，吐出一个可以直接安装的 `frames2d` 宠物目录：抠好背景的帧序列、映射好七个活动阶段的 `pet.json`、循环预览图，并且在交付前用插件自己的校验代码把成品再验一遍。

名字取自鲸鱼喷水 —— 把静止的东西喷出一口气，让它动起来。

> **English quick start**
>
> ```sh
> pip install -r requirements.txt
> python spout.py states  ./my-art          # check pose discovery
> python spout.py hatch   ./my-art --id my-pet --name "My Pet" --license MIT
> python spout.py install ./build/my-pet --force --select
> python spout.py validate "$DSH_HOME/pets/my-pet"
> python test_spout.py                      # 19 assertions, incl. the checks that must fail
> ```
>
> The knock-out defines background by **reachability from the image border**, not by
> brightness — so a pale face or a white shirt enclosed by line art stays opaque instead
> of being punched out. Every hatch is then re-validated, including a per-frame scan for
> holes punched through the character and a load through dsh-pet's own registry code.

```
静态立绘  →  抠背景  →  统一画布  →  呼吸/摆动微动画
         →  frames2d 帧序列  →  pet.json  →  $DSH_HOME/pets/<id>/
```

## 为什么不是一句阈值就完事

按「像素离背景色多远」来抠图，会把角色的**脸、皮肤、白衬衫**一起抠掉 —— 它们本身也接近白色，于是被判成背景，宠物在深色界面上就变成一张透明的脸。

`spout` 改用**连通性**判断背景：只有**能从图片四边走进来的**区域才算背景。脸和白衬衫被线稿围住、四边进不去，所以留在主体里，并被强制全不透明。只有真正贴着外轮廓的那一圈像素才使用渐变 alpha，因此边缘依旧是抗锯齿的，而且**不会**多出一圈浅色描边。

抠完还不算完，每一步都要验：

- `validate` 会逐帧检查**主体内部有没有被挖穿的洞**（用同一套连通性思路：走不出去的低 alpha 区域就是洞）；
- 再把成品交给**插件自己的注册表代码**加载一遍，确认它真的能被识别成 `frames2d` 宠物。

这两道检查都来自真实事故：第一版抠图把脸掏空了，而当时的检查太弱没发现。现在挖一个洞出来会被当场抓住。

## 依赖

```
pip install -r requirements.txt     # pillow, numpy
```

Python ≥ 3.9。`validate` / `hatch` 末尾的「插件契约检查」还需要 **Node ≥ 22.6**（TS type stripping）以及已安装的 `@linxin666/dsh-pet`；缺任意一个只会降级成一条 WARN，不影响其它检查。

## 用法

### 1. 先看看姿势认没认出来

```
python spout.py states <立绘文件夹>
```

九个状态按文件名认，别名都行（`idle` / `stand` / `default`，`waving` / `wave` / `hi`，`failed` / `error` / `sad` …），也会自动去 `<文件夹>/previews/` 里找。文件名对不上就在文件夹里放一个 `spout.json` 手写映射（见下）。

### 2. 孵化

```
python spout.py hatch <立绘文件夹> \
    --id whale-boy \
    --name "鲸鱼少年" \
    --description "银发蓝瞳的鲸鱼少年。" \
    --license "LicenseRef-All-Rights-Reserved" \
    --out ./build/whale-boy
```

产物长这样：

```
build/whale-boy/
├── pet.json                 # frames2d manifest（自动生成）
├── frames/                  # 9 条轨道
│   ├── idle/idle_01.webp …
│   ├── waiting/…
│   └── jumping/…
└── previews/                # 每条轨道一个循环预览 + source/ 原始立绘
```

常用开关：

| 开关 | 作用 |
|---|---|
| `--motion-preset default\|still` | `default` 给每条轨道加呼吸/摆动；`still` 每条轨道只留一帧（完全不动的保底方案） |
| `--motion <json>` | 覆盖单条轨道的动作关键帧 |
| `--canvas W H` / `--pad X Y` | 输出画布与内边距（默认 `384x480`，`10 6`） |
| `--barrier N` | 「明显不是背景」的颜色距离阈值（默认 55）。背景抠不干净就调小 |
| `--edge CLEAR OPAQUE` | alpha 渐变区间：距离 ≤CLEAR 视为背景，≥OPAQUE 视为实心（默认 `6 60`） |
| `--force-knockout` | 原图带 alpha 也强制重新抠 |
| `--previews webp,gif` | 预览格式 |
| `--jobs N` | 并行抠图进程数 |

### 3. 安装

```
python spout.py install ./build/whale-boy --force --select
```

装到 `$DSH_HOME/pets/<id>/`；`--select` 顺带把它设为当前宠物（会先备份 `pet.json`）。

> **插件只在 `dsh web` 启动时扫描一次宠物注册表** —— 安装新宠物后必须重启，刷新页面不够。

### 4. 随时复验

```
python spout.py validate "$DSH_HOME/pets/whale-boy"
python spout.py validate <目录> --no-plugin-check   # 跳过插件契约检查
```

### 调试单个图

```
python spout.py mask idle.gif --out idle.png
```

会打印不透明像素数、洞的像素数，并给出诊断（`--barrier` 调小 / `--edge` 调大）。

## spout.json（可选）

放在立绘文件夹里，省掉一堆命令行参数，并可以手写姿势映射和动作覆盖：

```jsonc
{
  "id": "whale-boy",
  "displayName": "鲸鱼少年",
  "description": "银发蓝瞳的鲸鱼少年。",
  "license": "LicenseRef-All-Rights-Reserved",
  "author": "lcq",
  "version": "1.0.0",

  "poses": {                       // 文件名认不出来时手写映射
    "idle": "previews/stand-still.png",
    "running": "previews/think.gif"
  },

  "motionPreset": "default",
  "motion": {                      // 覆盖某条轨道的关键帧
    "idle": { "ms": 180, "keys": [
      { "dy": -4, "sy": 0.99, "angle": -1 },
      { "dy": 0,  "sy": 1.0,  "angle": 0.5 }
    ]}
  },

  "phases": {                      // 覆盖活动阶段 → 轨道 的映射
    "doing": "running"
  }
}
```

一个关键帧的含义：把该状态的立绘按 `dy` 上下平移、按 `sx/sy` 绕**脚底**缩放、按 `angle` 绕脚底旋转，得到一张输出帧。`ms` 是这一帧的停留毫秒数。省略的字段用默认值（`dy=0, sx=sy=1, angle=0`）。

## 九个状态与七个阶段

`frames2d` 的轨道名，以及默认的阶段映射：

| 阶段 | 轨道 | 什么时候播 |
|---|---|---|
| `idle` | `idle` | 空闲 |
| `waiting` | `waiting` | 等待用户 |
| `thinking` | `running` | 模型思考中 |
| `tool` | `running-right` | 调用工具中 |
| `review` | `review` | 组织回复 |
| `done` | `waving` | 本轮完成 |
| `failed` | `failed` | 出错 |

轨迹名固定为：`idle`、`running-right`、`running-left`、`waving`、`jumping`、`failed`、`waiting`、`running`、`review`。

## 自测

```
python test_spout.py
```

跑 6 组用例：平板背景抠图、自带 alpha 直通、`still` 预设、注入空洞必须被抓到、缺帧必须报错、孤立帧必须报错、非法 id 必须拒绝。

## 注意

- 抠图要求立绘背景是**一块纯色**（白底最常见）。背景色自动取图片外圈像素的中位数，所以非白底也能用。
- 角色**外轮廓**需要有线稿或色块把内外隔开，否则泛洪会从缺口漏进角色内部。这是所有「按连通性抠图」的共同前提。
- alpha 已存在的图（透明 PNG/GIF）默认直接使用，不再抠除。
- 输出的是 `frames2d` 宠物，不需要重建插件、不需要发布 npm 包 —— 一个目录 + `pet.json` 就够了。

## 许可

本工具以 [MIT](LICENSE) 发布。

**宠物素材的授权由素材作者决定，与工具无关** —— 用 `--license` 如实声明。`dsh-pet` 的 v2 契约要求每个宠物带来源信息（`license` 字段是必填的），`spout` 会在你没指定时写入 `LicenseRef-All-Rights-Reserved`，提醒你不要默认放开素材权利。仓库本身不含任何宠物立绘，请自行保管素材来源与授权。

## 相关

- [`dsh-pet`](https://github.com/zhu1090093659/dsh-web) —— 本工具产出所服务的 DSH 宠物插件
- `frames2d` manifest 契约：`contracts/pet-manifest-v2.schema.json`，权威校验器 `src/manifest-v2.ts`
