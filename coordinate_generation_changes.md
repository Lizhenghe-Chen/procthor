# 坐标生成改动说明（面向 Unity 解析兼容）

本文档用于解释本仓库中与坐标生成相关的最小改动，目标是让生成的 JSON 与 Unity 侧解析逻辑一致，避免墙面错位、窗户排布混乱、物体悬空。

## 机器可读摘要

- 目标系统: Unity 端场景解析与实例化
- 核心约束: Unity 直接使用 JSON 中的 position 作为 transform 位置
- 改动策略: 只改坐标语义不一致处，不重构流程
- 影响文件:
  - procthor/generation/generation.py
  - procthor/generation/house.py
  - procthor/generation/wall_objects.py
  - procthor/generation/objects.py

## 背景与问题

在原始生成逻辑中，部分数据采用了中心点语义或旧墙体顶点顺序；而 Unity 端按当前解析方式直接消费这些值，导致以下问题：

1. 墙体四边形顶点顺序与下游假设不一致。
2. 窗户沿墙定位使用了旧索引，导致偏移或翻转异常。
3. 地面物体使用中心高度，进入 Unity 后表现为整体抬高。
4. 资产组父物体保持原始高度，出现父物体悬空。

## 改动 1：墙体 polygon 顶点顺序修正

文件:

- procthor/generation/generation.py
- procthor/generation/house.py

改动内容:

- 墙体 polygon 从跨边顺序改为顺序连接的四边形顺序。

修正前顶点语义:

1. 底部起点
2. 底部终点
3. 顶部起点
4. 顶部终点

修正后顶点语义:

1. 底部起点
2. 顶部起点
3. 顶部终点
4. 底部终点

作用:

- 避免四边形在几何上被错误解释。
- 统一后续墙体跨度、方向与开孔定位的基础坐标。

## 改动 2：窗户沿墙定位索引对齐新墙体顺序

文件:

- procthor/generation/wall_objects.py

改动内容:

- 在 add_windows 中，墙体底边端点索引从 0 和 1 改为 0 和 3。
- 具体是墙轴判断与起始偏移计算都同步使用新索引。

作用:

- 与新 polygon 顺序一致。
- 保证窗洞在墙体上的一维坐标映射正确。

## 改动 3：普通地面物体 Y 改为地面基准

文件:

- procthor/generation/objects.py

改动内容:

- 在 ProceduralRoom.sample_place_asset_in_rectangle 中，普通资产位置的 y 从 半高 改为 0。

作用:

- 适配 Unity 直接使用 position 的行为。
- 消除地面物体整体抬高问题。

## 改动 4：资产组父物体 Y 归零，子物体高度保留

文件:

- procthor/generation/objects.py

改动内容:

- 在 AssetGroup.assets_dict 中，将资产组顶层对象的 position.y 统一设为 0。
- 子物体维持自身相对高度，不做同样归零。

作用:

- 修复资产组父物体悬空。
- 保持台灯、摆件等放置在父物体表面的相对高度关系。

## 与 git 变更的一致性核验

已按 git diff 核对，上述 4 项与实际差异一致。

额外说明:

- procthor/generation/house.py 中存在 moviepy 的导入路径改动。
- 该改动与坐标语义无关，不属于本说明的核心范围。

## 给 LLM 的实现规则（可直接复用）

如果后续继续维护同类代码，请遵循以下规则：

1. 墙体 polygon 一律按顺序连接顶点，且可明确定位底边两个端点。
2. 任何沿墙一维定位都必须基于底边端点，不要混用顶部点索引。
3. 若消费端直接写 transform.position，则生成端不要再输出中心点高度。
4. 资产组父子物体要分开处理高度：父物体贴地，子物体保留相对高度。
5. 坐标改动后必须用固定 seed 回归检查至少三类对象：墙体、窗户、资产组父物体。

## 验证清单

1. 墙体是否完整闭合且方向稳定。
2. 窗户是否落在对应墙段，且左右翻转符合预期。
3. 普通地面物体底部是否贴地。
4. 资产组父物体是否贴地，子物体是否仍在父物体表面。

