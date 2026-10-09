"""归并逻辑评估 —— 直接 import 正式实现 scraper/dedup.py，在人工标注真值上验证判定质量。

标注集 123 对 = 包含度 >= 0.50 的全集 100 对 + [0.40, 0.50) 敏感性带 23 对。
两条线：0.50 系统线（系统会看一眼）/ 0.40 标注线（实测同款最低分，见 census 脚本）。

核心结论：三种相似度指标全部不可分；硬门槛压低误判上界但两类仍重叠；
零错归并靠「硬门槛 + 阈值 + 灰色地带」三层配合（阈值 0.75，高于过门槛假阳性上限 0.667，裕度 0.083）。
标注对用精确锚定（先全等再子串，命中数 ≠ 1 直接报错）；完整事实见 docs/可行性评估.md。

用法：python spike/evaluate_merge.py
"""

from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.dedup import (  # noqa: E402
    GREY_ZONE_THRESHOLD,
    MERGE_THRESHOLD,
    Candidate,
    Decision,
    ExistingProduct,
    build_signature,
    containment,
    decide,
)

CORPUS = Path(__file__).parent / "corpus.json"

# --- 人工标注的真值（逐对读出；标准：是不是同一款商品，SKU 层面）---
LABELS: list[tuple[str, str, bool, str]] = [
    # -------------------------------------------------------------------
    # 真阳性：同一商品，措辞/详略不同
    # -------------------------------------------------------------------
    (
        "FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C",
        "FreeClip 2 耳夹式耳机",
        True,
        "同款：后者省略了颜色和接口",
    ),
    (
        "FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C",
        "freeclip2 耳夹式蓝牙耳机 冰莓紫",
        True,
        "同款：仅大小写和空格差异",
    ),
    (
        "Pura X Max 12GB+256GB",
        "HUAWEI 华为 Pura X Max 手机",
        True,
        "同款：后者省略规格（★ 这条是严格相等的代价，见下方结论）",
    ),
    (
        "iPhone 18 Pro Max 5G手机 256GB 勃艮第酒红色",
        "Apple 苹果 iPhone 18 Pro 5G手机",
        False,
        "★ 不同商品：Pro vs Pro Max 是两个 SKU（只差一个后缀词）",
    ),
    # -------------------------------------------------------------------
    # 假阳性候选：品牌相同，文本相似，但是不同商品
    # -------------------------------------------------------------------
    (
        "iPhone 18 Pro 5G手机",
        "iPhone Duo 5G手机",
        False,
        "★ 最危险：相似度 0.727~0.800，但型号词完全不同",
    ),
    (
        "FreeClip 2 耳夹式耳机",
        "FreeClip 开放式设计",
        False,
        "★ 次危险：文本极像，但一代 vs 二代（差一个数字）",
    ),
    (
        "FreeClip 2 耳夹式耳机",
        "FreeClip 耳夹耳机 UltraCareX",
        False,
        "同上：FreeClip 2 vs FreeClip 一代配饰装",
    ),
    (
        "百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安",
        "酷态科 充电宝10号电能棒电芯10000毫安",
        True,
        "同款：仅促销前缀差异（★ 锚定报错时从语料里发现的真实重复爆料）",
    ),
    (
        "百亿补贴：酷态科 CP13电能块自带线电芯10000毫安",
        "百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安",
        False,
        "同品牌同容量，型号不同（10号 vs CP13）",
    ),
    (
        "5折 舒适缓震 女士跑鞋",
        "5折 Slip-ins闪穿 男士健步鞋",
        False,
        "同品牌同折扣，品类词不同",
    ),
    (
        "米良品 家用便携可折叠拖轮购物车",
        "米良品 创意电动发光自吸掏耳神器",
        False,
        "同品牌，完全不同品类",
    ),

    # 2026-10-08 补齐：>= 0.50 全标穷尽 100 对（census 断言覆盖率 100%）；
    # [0.40, 0.50) 敏感性带 +23 对（同款最低分 0.400，两条线的来路不同）。
    # 合计 123 对；判法与前 11 对一致，复核结果见 docs/可行性评估.md 12.4 节末。
    ('百亿补贴：酷态科 CP13电能块自带线电芯10000毫安', '酷态科 CP13电能块自带线电芯10000毫安', True, '同款：仅促销前缀差异'),
    ('OPPO Find X9s Pro 手机', 'OPPO Find X9s Pro 5G手机 12GB+512GB 自在白', True, '同款：后者补了规格与配色（沿用"Pura 那条"同一判法）'),
    ('华为 freeclip2 耳夹式蓝牙耳机 冰莓紫', '华为 FreeClip 2 耳夹式耳机', True, '同款：同一副耳机的两种写法'),
    ('华为 freeclip2 耳夹式蓝牙耳机 冰莓紫', '华为 FreeClip 开放式设计 耳夹式蓝牙耳机', False, '不同代：二代 vs 一代（开放式设计）'),
    ('OPPO Find X9 Ultra 5G手机 12GB+256GB 大地苔原', 'OPPO Find X9s Pro 5G手机 12GB+512GB 自在白', False, '不同型号：X9 Ultra vs X9s Pro'),
    ('斯凯奇 5折 女士轻质老爹鞋', '斯凯奇 5折 舒适缓震 女士跑鞋', False, '斯凯奇：老爹鞋 vs 跑鞋'),
    ('斯凯奇 5折 女士轻质老爹鞋', '斯凯奇 5折 男鞋秋季休闲皮鞋', False, '斯凯奇：女鞋 vs 男鞋'),
    ('斯凯奇 5折 女士轻质老爹鞋', '斯凯奇 5折 女鞋摩卡熊 复古老爹鞋', False, '斯凯奇：两双不同的鞋'),
    ('斯凯奇 5折 女士轻质老爹鞋', '斯凯奇 5折 男厚底增高缓震老爹鞋', False, '斯凯奇：女鞋 vs 男鞋'),
    ('斯凯奇 5折 女士轻质老爹鞋', '斯凯奇 5折 Slip-ins闪穿 男士健步鞋', False, '斯凯奇：女鞋 vs 男鞋'),
    ('斯凯奇 5折 舒适缓震 女士跑鞋', '斯凯奇 5折 男鞋秋季休闲皮鞋', False, '斯凯奇：女鞋 vs 男鞋'),
    ('斯凯奇 5折 舒适缓震 女士跑鞋', '斯凯奇 5折 男厚底增高缓震老爹鞋', False, '斯凯奇：女鞋 vs 男鞋'),
    ('斯凯奇 5折 男鞋秋季休闲皮鞋', '斯凯奇 5折 女鞋摩卡熊 复古老爹鞋', False, '斯凯奇：男鞋 vs 女鞋'),
    ('斯凯奇 5折 男鞋秋季休闲皮鞋', '斯凯奇 5折 男厚底增高缓震老爹鞋', False, '斯凯奇：皮鞋 vs 老爹鞋'),
    ('斯凯奇 5折 男鞋秋季休闲皮鞋', '斯凯奇 5折 Slip-ins闪穿 男士健步鞋', False, '斯凯奇：皮鞋 vs 健步鞋'),
    ('斯凯奇 5折 女鞋摩卡熊 复古老爹鞋', '斯凯奇 5折 男厚底增高缓震老爹鞋', False, '斯凯奇：女鞋 vs 男鞋'),
    ('Apple iPhone 18 Pro Max 5G手机 256GB 勃艮第酒红色', 'Apple 苹果 iPhone Duo 5G手机', False, '不同 SKU：Pro Max vs Duo'),
    ('斯凯奇 5折 男厚底增高缓震老爹鞋', '斯凯奇 5折 Slip-ins闪穿 男士健步鞋', False, '斯凯奇：老爹鞋 vs 健步鞋'),
    ('Apple 苹果 iPhone 18 Pro 5G手机', 'Apple/苹果 妙控键盘', False, '完全不同品类：手机 vs 键盘'),
    ('HUAWEI 华为 Pura X Max 手机', 'HUAWEI 华为 畅享 90 Pro Max 手机', False, '不同系列：Pura X Max vs 畅享 90 Pro Max'),
    ('Apple 苹果 iPhone Duo 5G手机', 'Apple/苹果 妙控键盘', False, '完全不同品类：手机 vs 键盘'),
    ('Apple/ 苹果 AirPodsPro 2代配USB-C充电盒蓝牙无线耳机JV3【10天内】', 'Apple/苹果 妙控键盘', False, '完全不同品类：耳机 vs 键盘'),
    ('移动端：极空间 智能AI NAS网络存储 16GB 0块', '移动端：极空间 八核四盘位 NAS网络存储', False, '极空间：不同型号（四盘位 vs 未标明）'),
    ('移动端：极空间 智能AI NAS网络存储 16GB 0块', 'ZSpace/极空间 Nas网络存储', False, '后者是泛化标题，无型号可证同一'),
    ('移动端：极空间 八核四盘位 NAS网络存储', 'ZSpace/极空间 Nas网络存储', False, '后者是泛化标题，无型号可证同一'),
    ('ZSpace/极空间 Nas网络存储', 'ZSpace 极空间 私有云Z2Pro 四核Nas网络存储服务器', False, 'Z2Pro vs 泛化标题，不同型号'),
    ('OPPO Find X9 Ultra 5G手机 12GB+256GB 大地苔原', 'vivo X300s 5G手机 12GB+256GB 梦核紫', False, '不同品牌：OPPO vs vivo'),
    ('vivo X300s 5G手机 12GB+256GB 梦核紫', 'OPPO Find X9s Pro 5G手机 12GB+512GB 自在白', False, '不同品牌：vivo vs OPPO'),
    ('OPPO Find X9 Ultra 5G手机 12GB+256GB 大地苔原', 'OPPO Find X9s Pro 手机', False, '不同型号：X9 Ultra vs X9s Pro'),
    ('荣耀 Magic8 Pro 5G手机 第五代骁龙8至尊版 12+512', 'OPPO Find X9s Pro 5G手机 12GB+512GB 自在白', False, '不同品牌：荣耀 vs OPPO'),
    ('海澜之家 100%绵羊毛澳洲羊毛衫（门店有售）', '海澜之家 男士行政翻领棉服（门店有售）', False, '同品牌不同品类：羊毛衫 vs 棉服'),
    ('海澜之家 100%绵羊毛澳洲羊毛衫（门店有售）', '海澜之家 男士白鸭绒90绒轻薄羽绒服', False, '同品牌不同品类：羊毛衫 vs 羽绒服'),
    ('米良品 家用便携可折叠拖轮购物车', '米良品 手机专用摄影辅助偏振镜', False, '同品牌完全不同品类'),
    ('米良品 家用便携可折叠拖轮购物车', '米良品 家用多档位桌面吸尘清洁神器', False, '同品牌完全不同品类'),
    ('米良品 家用便携可折叠拖轮购物车', '米良品 家用门窗隔音密封条 250*2cm 2卷装', False, '同品牌完全不同品类'),
    ('神舟 战神T8 16英寸游戏本笔记本电脑(i7-14700HX 16G 1TB 180HZ)', '彩棠 浮光掠影修容盘 16g', False, '不同品牌不同品类'),
    ('海澜之家 男士行政翻领棉服（门店有售）', '海澜之家 男士白鸭绒90绒轻薄羽绒服', False, '同品牌不同品类：棉服 vs 羽绒服'),
    ('mikibobo 多效合一机洗专用 5D多腔洗衣凝珠 2盒装-洗衣液款（2*300g/盒）', '友谊 雪花膏18.5g', False, '不同品牌不同品类'),
    ('五粮液 盛世龙年 52%vol 浓香型白酒 500ml*2瓶 白酒礼盒送礼', '京东京造 净澈去屑玉龙茶香洗发水 500ml', False, '不同品牌不同品类'),
    ('五粮液 盛世龙年 52%vol 浓香型白酒 500ml*2瓶 白酒礼盒送礼', '泸州老窖 浓香型 52度 500mL 2瓶', False, '同品类不同品牌：五粮液 vs 泸州老窖'),
    ('五粮液 盛世龙年 52%vol 浓香型白酒 500ml*2瓶 白酒礼盒送礼', '烧春酒坊 200年纪念版 酱香型 53%vol 500mL 2瓶', False, '同品类不同品牌'),
    ('京东京造 净澈去屑玉龙茶香洗发水 500ml', '京东京造 净澈蓬松栀子香洗发水 800ml', False, '同品牌不同香型/容量，两个 SKU'),
    ('京东京造 净澈去屑玉龙茶香洗发水 500ml', '京东京造 护卷蓬松造型摩丝 200ml', False, '同品牌不同品类：洗发水 vs 摩丝'),
    ('京东京造 净澈去屑玉龙茶香洗发水 500ml', '泸州老窖 浓香型 52度 500mL 2瓶', False, '不同品牌不同品类'),
    ('京东京造 净澈去屑玉龙茶香洗发水 500ml', '烧春酒坊 200年纪念版 酱香型 53%vol 500mL 2瓶', False, '不同品牌不同品类'),
    ('京东京造 净澈蓬松栀子香洗发水 800ml', '京东京造 护卷蓬松造型摩丝 200ml', False, '同品牌不同品类'),
    ('斯凯奇 5折 舒适缓震 女士跑鞋', '斯凯奇 5折 女鞋摩卡熊 复古老爹鞋', False, '斯凯奇：跑鞋 vs 老爹鞋'),
    ('米良品 创意电动发光自吸掏耳神器', '米良品 手机专用摄影辅助偏振镜', False, '同品牌完全不同品类'),
    ('米良品 创意电动发光自吸掏耳神器', '米良品 家用多档位桌面吸尘清洁神器', False, '同品牌完全不同品类'),
    ('米良品 创意电动发光自吸掏耳神器', '米良品 家用门窗隔音密封条 250*2cm 2卷装', False, '同品牌完全不同品类'),
    ('艺乐雅 陶瓷一体盆浴室柜 家用组合洗漱台 智能背光方镜 70cm 包安装/入户', '艺乐雅 浴室柜陶瓷一体盆 家用组合洗漱台 奶白款-智能月牙镜-80cm 包安装/上门', False, '同系列不同 SKU：70cm 方镜 vs 80cm 月牙镜（尺寸即 SKU）'),
    ('友谊 雪花膏18.5g', '联想 拯救者Y9000P 2026至尊版 游戏笔记本电脑(Ultra 9 290HX Plus 32G 1T RTX5070Ti 2.5K OLED) 国补', False, '不同品牌不同品类'),
    ('完达山 全脂纯牛奶 盒装 高温灭菌 营养均衡 200ml 24盒', '京东京造 护卷蓬松造型摩丝 200ml', False, '不同品牌不同品类'),
    ('移动端、88VIP：巴黎欧莱雅 精油润养洗发水露套装 2件', '88VIP：马奇新新 马来西亚柠檬黄油夹心饼干礼盒休闲零食点心路餐', False, '不同品牌不同品类'),
    ('移动端、88VIP：巴黎欧莱雅 精油润养洗发水露套装 2件', '移动端、88VIP：真彩 24色丙烯马克笔小不透色可叠色美术', False, '不同品牌不同品类'),
    ('斯凯奇 5折 女鞋摩卡熊 复古老爹鞋', '斯凯奇 5折 Slip-ins闪穿 男士健步鞋', False, '斯凯奇：女鞋 vs 男健步鞋'),
    ('海尔 XQB80-Z10D0 波轮洗衣机 8kg 博卡6键自编程 一级能效 京东自营', '海尔 XQB100-BZ2GD0 变频波轮洗衣机 10KG 直驱变频 漩瀑洗 除菌螨 博卡灰 一级能效 家用大容量', False, '海尔：XQB80-Z10D0 8kg vs XQB100-BZ2GD0 10kg，两个型号'),
    ('泸州老窖 浓香型 52度 500mL 2瓶', '烧春酒坊 200年纪念版 酱香型 53%vol 500mL 2瓶', False, '同品类不同品牌'),
    ('得力 TD814 儿童手工文具套装 剪刀美工刀胶棒 4件套', '得力 彩色中性笔', False, '同品牌不同品类：手工套装 vs 中性笔'),
    ('88VIP：马奇新新 马来西亚柠檬黄油夹心饼干礼盒休闲零食点心路餐', '移动端、88VIP：真彩 24色丙烯马克笔小不透色可叠色美术', False, '不同品牌不同品类'),
    ('华为 FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C', '华为 FreeClip 耳夹耳机 UltraCareX钻扣配饰', False, '耳机本体 vs 钻扣配饰；且一代 vs 二代'),
    ('华为 FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C', '华为 FreeClip 开放式设计 耳夹式蓝牙耳机', False, '不同代：二代 vs 一代'),
    ('百亿补贴：诺希 35W双口快充 氮化镓充电器', '百亿补贴、今日必买：诺希 35W双口氮化镓充电器15苹果17/16快充适用华为安卓荣耀充电头', True, '同款：后者是同一充电器堆了 SEO 关键词'),
    ('百亿补贴：酷态科 65W氮化镓三口PD快充充电器', '百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安', False, '同品牌不同品类：充电器 vs 充电宝'),
    ('百亿补贴：酷态科 65W氮化镓三口PD快充充电器', '酷态科 充电宝10号电能棒电芯10000毫安', False, '同品牌不同品类：充电器 vs 充电宝'),
    ('百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安', '25号SE实测、90W卡片充 | 酷态科/CukTech9.28日报', False, '后者是资讯日报，不是商品'),
    ('百亿补贴：酷态科 充电宝10号电能棒电芯10000毫安', '酷态科 CP13电能块自带线电芯10000毫安', False, '同品牌同容量，型号不同'),
    ('百亿补贴：酷态科 CP13电能块自带线电芯10000毫安', '酷态科 充电宝10号电能棒电芯10000毫安', False, '同品牌同容量，型号不同'),
    ('华为 Free chip2 耳夹式无线蓝牙耳机', '华为 FreeClip 2 耳夹式耳机', True, '同款："Free chip2" 是 FreeClip 2 的讹写'),
    ('华为 freeclip2 耳夹式蓝牙耳机 冰莓紫', '华为 FreeClip 耳夹耳机 UltraCareX钻扣配饰', False, '耳机 vs 配饰，且一代 vs 二代'),
    ('华为 FreeClip 耳夹耳机 UltraCareX钻扣配饰', '华为 FreeClip 开放式设计 耳夹式蓝牙耳机', False, '配饰 vs 耳机本体'),
    ('米良品 手机专用摄影辅助偏振镜', '米良品 家用多档位桌面吸尘清洁神器', False, '同品牌完全不同品类'),
    ('米良品 手机专用摄影辅助偏振镜', '米良品 家用门窗隔音密封条 250*2cm 2卷装', False, '同品牌完全不同品类'),
    ('25号SE实测、90W卡片充 | 酷态科/CukTech9.28日报', '酷态科 充电宝10号电能棒电芯10000毫安', False, '后者是资讯日报，不是商品'),
    ('倍思 超能充三合一充电宝器 自带线插头 20000mAh 移动电源', 'PISEN/品胜 三屏充电宝 自带双线 30W快充 20000mAh 移动电源', False, '不同品牌：倍思 vs 品胜'),
    ('华帝 JZT-i10123D 5.2kW大火力 天然气嵌入式燃气灶', '华帝 超薄平嵌抽油烟机燃气灶具套装', False, '单件灶具 vs 烟灶套装，不是同一个 SKU'),
    ('酷态科 充电宝10号电能棒电芯10000毫安', '酷态科 CP13电能块自带线电芯10000毫安', False, '同品牌同容量，型号不同'),
    ('极空间 私有云 T6 六盘位 NAS存储（intel、16GB、空盘）', '移动端：极空间 智能AI NAS网络存储 16GB 0块', False, '极空间：T6 六盘位 vs 未标明型号'),
    ('UGREEN/绿联 万兆 NAS网络存储器', 'UGREEN 绿联 私有云DXP4800 GT 8G四盘位NAS网络存储', False, '绿联：万兆款 vs DXP4800 GT，两个型号'),
    ('米良品 家用多档位桌面吸尘清洁神器', '米良品 家用门窗隔音密封条 250*2cm 2卷装', False, '同品牌完全不同品类'),
    ('得力 400页4色简约便利贴便签纸76*76mm便签本/记事贴/自粘留言本办公用品BT251', '得力 彩色中性笔', False, '同品牌不同品类'),
    ('得力 deli）18色可水洗水彩笔易握粗三角学生儿童涂色颜色马克笔画笔套装文具70721水彩笔开学礼物男女孩', '得力 彩色中性笔', False, '同品牌不同品类'),
    ('今日必买：得力 AR智能3D立体 三色无极调光充电款地球仪', '得力 彩色中性笔', False, '同品牌不同品类'),
    ('今日必买：得力 易擦少屑 橡皮擦', '得力 彩色中性笔', False, '同品牌不同品类：橡皮擦 vs 中性笔'),
    ('得力 BDHB5-07 B5活页本 绿色 50张横线', '得力 彩色中性笔', False, '同品牌不同品类'),
    ('今日必买：得力 开学必备用品三顾喵庐秒干按动中性笔 0.5mmST笔头学生刷题笔考试笔 黑色4支/盒SA344高考考试', '得力 彩色中性笔', False, '同品牌不同品类'),
    ('得力 NB560-4 B5活页替芯 26孔隐形定位线 60张', '得力 彩色中性笔', False, '同品牌不同品类'),
    ('得力 可单手换页跨页书写 活页本 B5/40张/透明', '得力 彩色中性笔', False, '同品牌不同品类'),
    ('得力 时尚羊巴皮 笔记本 笔插 A5/64张棕色', '得力 彩色中性笔', False, '同品牌不同品类：笔记本 vs 中性笔'),

    # [0.40, 0.50) 敏感性带（+23 对）："Free chip2" 讹写把同款拖到 0.400，标注线因此下探到 0.40。
    ('OPPO Find X9 Ultra 5G手机 12GB+256GB 大地苔原', '华为 Pura X Max 12GB+256GB 橄榄金小艺伴随式AI 华为大阔折叠手机', False, '不同品牌旗舰：OPPO vs 华为'),
    ('华为 Pura X Max 12GB+256GB 橄榄金小艺伴随式AI 华为大阔折叠手机', 'vivo X300s 5G手机 12GB+256GB 梦核紫', False, '不同品牌旗舰：华为 vs vivo'),
    ('极空间 私有云 T2 2盘位NAS存储（RK 3568、4GB）', '极空间 私有云 T6 六盘位 NAS存储（intel、16GB、空盘）', False, '极空间：2盘位 vs 6盘位，两个型号'),
    ('荣耀 Magic8 Pro 5G手机 第五代骁龙8至尊版 12+512', 'Apple 苹果 iPhone 18 Pro 5G手机', False, '不同品牌：荣耀 vs Apple'),
    ('OPPO Find X9s Pro 5G手机 12GB+512GB 自在白', 'Apple 苹果 iPhone 18 Pro 5G手机', False, '不同品牌：OPPO vs Apple'),
    ('华为 FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C', '优越者 Type-C3.2接口 M.2 NVMe固态硬盘盒', False, '完全不同品类：耳机 vs 硬盘盒'),
    ('Apple 苹果 iPhone 18 Pro 5G手机', '小米 18 Fold 玄戒O3 AI处理器 5G手机', False, '不同品牌：Apple vs 小米'),
    ('Apple 苹果 iPhone 18 Pro 5G手机', '【评论有奖】60W 不重要，关键是 AVS！带 AVS 才能低温快充，聊聊 iPhone18 Pro 要不要买 329 元原装头，评论赢奖金', False, '后者是资讯文章，不是商品'),
    ('移速 能量魔方 70000mAh 35W快充 移动电源', 'PISEN/品胜 三屏充电宝 自带双线 30W快充 20000mAh 移动电源', False, '不同品牌：移速 vs 品胜'),
    ('极空间 私有云 T2 2盘位NAS存储（RK 3568、4GB）', '极空间 Q4 NAS存储（8GB、无硬盘）', False, '极空间：T2 vs Q4，两个型号'),
    ('极空间 私有云 T6 六盘位 NAS存储（intel、16GB、空盘）', '极空间 Q4 NAS存储（8GB、无硬盘）', False, '极空间：T6 vs Q4，两个型号'),
    ('铠侠 隼闪系列 TransMemory U301 USB 3.2 U盘 USB-A', '威刚 UV128 USB3.2高速 U盘', False, '不同品牌：铠侠 vs 威刚'),
    ('得力 BDHB5-07 B5活页本 绿色 50张横线', '得力 可单手换页跨页书写 活页本 B5/40张/透明', False, '同系列不同 SKU：50张 vs 40张，页数即 SKU'),
    ('OPPO Find X9 Ultra 5G手机 12GB+256GB 大地苔原', 'Apple iPhone 18 Pro Max 5G手机 256GB 勃艮第酒红色', False, '不同品牌：OPPO vs Apple'),
    ('雷克沙 雷神THOR PRO 固态硬盘 M.2接口 PCIe 4.0x4 SLC智能缓存 雷神Thor Pro 0.5TB', '首个AI原生NAS操作系统TOS 7｜铁威马F4-425 Pro', False, '完全不同品类：固态硬盘 vs NAS'),
    ('联想 拯救者Y9000P 2026至尊版 游戏笔记本电脑(Ultra 9 290HX Plus 32G 1T RTX5070Ti 2.5K OLED) 国补', '闪昇 P32 USB3.1 TypeC 2TB 3.5英寸移动机械硬盘', False, '完全不同品类：笔记本 vs 移动硬盘'),
    ('Apple iPhone 18 Pro Max 5G手机 256GB 勃艮第酒红色', 'vivo X300s 5G手机 12GB+256GB 梦核紫', False, '不同品牌：Apple vs vivo'),
    ('Apple iPhone 18 Pro Max 5G手机 256GB 勃艮第酒红色', 'OPPO Find X9s Pro 5G手机 12GB+512GB 自在白', False, '不同品牌：Apple vs OPPO'),
    ('LG 27G810A 4K 180Hz IPS 27英寸 显示器', 'LG 27GX700A OLED第四代堆叠技术 黑色 26.5英寸 显示器', False, '同品牌不同型号：IPS 27英寸 vs OLED 26.5英寸'),
    ('雷蛇 炼狱蝰蛇 V4 PRO 2.4G无线鼠标 45000DPI 白色', '首个AI原生NAS操作系统TOS 7｜铁威马F4-425 Pro', False, '完全不同品类：鼠标 vs NAS'),
    ('华为 FreeClip 2 耳夹式多单元动圈蓝牙耳机 冰莓紫 Type-C', '华为 Free chip2 耳夹式无线蓝牙耳机', True, '同款："Free chip2" 讹写，与带规格写法是同一副耳机'),
    ('华为 Free chip2 耳夹式无线蓝牙耳机', '华为 freeclip2 耳夹式蓝牙耳机 冰莓紫', True, '同款："Free chip2" 讹写的另一种拼法（沿用 #78 判法）'),
    ('铠侠 隼闪系列 TransMemory U301 USB 3.2 U盘 USB-A', '金士顿 DTDEG2 双接口 U盘', False, '不同品牌：铠侠 vs 金士顿'),
]


# --- 语料加载与精确锚定 ---


def load_titles() -> list[str]:
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    return [c["title"] for c in raw if c.get("title")]


def resolve(titles: list[str], prefix: str) -> str:
    """把标注文本锚定到【唯一】一条语料标题：先全等，再子串（语料里有互为子串的真阳性对）；
    命中 0 条或多条都直接报错——宁可 loud failure，不要 silent wrong。
    """
    exact = [t for t in titles if t.strip() == prefix.strip()]
    hits = exact if exact else [t for t in titles if prefix in t]
    if len(hits) != 1:
        raise SystemExit(
            f"\n❌ 锚定失败：{prefix!r} 在语料里命中 {len(hits)} 条（需要恰好 1 条）\n"
            + "\n".join(f"    {h}" for h in hits[:5])
            + "\n   → 请检查标注文本，或确认语料是否变了。\n"
        )
    return hits[0]


# --- 评估 ---


def make_existing(idx: int, title: str) -> ExistingProduct:
    norm, model_toks, toks = build_signature(
        Candidate(source="smzdm", source_id=str(idx), title=title)
    )
    return ExistingProduct(
        product_id=idx,
        title_norm=norm,
        brand=None,          # 品牌一律不提供（最悲观设定）：硬门槛只剩型号 token 在工作
        model_tokens=model_toks,
        tokens=toks,
    )


def evaluate(titles: list[str]) -> None:
    print("=" * 108)
    print(f"归并逻辑评估 —— 直接调用 scraper/dedup.py 的正式实现")
    print(f"语料：{len(titles)} 条真实标题   |   人工标注：{len(LABELS)} 对")
    print(f"阈值：自动归并 ≥ {MERGE_THRESHOLD}   灰色地带 ≥ {GREY_ZONE_THRESHOLD}")
    print("=" * 108)
    print(f"{'标签':<6} {'包含度':>7} {'Dice':>7} {'Jacc':>7}  {'硬门槛':<7} {'判定':<7} {'结果':<5}  标题对")
    print("-" * 108)

    rows: list[dict] = []
    for i, (pa, pb, label, note) in enumerate(LABELS):
        ta, tb = resolve(titles, pa), resolve(titles, pb)

        existing = [make_existing(0, ta)]
        cand = Candidate(source="smzdm", source_id="1", title=tb)
        res = decide(cand, existing)

        _, model_a, toks_a = build_signature(
            Candidate(source="smzdm", source_id="0", title=ta)
        )
        _, model_b, toks_b = build_signature(cand)
        score = containment(toks_a, toks_b)
        gate_pass = model_a == model_b

        # 另外两种指标，用于说明"为什么最终没用它们"
        sa, sb = set(toks_a), set(toks_b)
        dice = 2 * len(sa & sb) / (len(sa) + len(sb)) if sa and sb else 0.0
        jacc = len(sa & sb) / len(sa | sb) if sa and sb else 0.0

        predicted_same = res.decision is Decision.MERGE

        if predicted_same == label:
            mark = "✅" if label else "✅"
        elif not predicted_same and label:
            mark = "🟡漏"     # 漏归并：安全，少了几个价格点
        else:
            mark = "❌错"     # 错归并：危险，污染价格序列

        gate_str = "通过" if gate_pass else "拦下"
        print(
            f"{'同款' if label else '不同':<6} {score:>7.3f} {dice:>7.3f} {jacc:>7.3f}  "
            f"{gate_str:<7} {res.decision.value:<7} {mark:<5}  {pa[:34]}"
        )
        print(f"{'':>44}{'':<7}{'':<7}  ↕ {note}")

        rows.append({
            "label": label,
            "score": score,
            "dice": dice,
            "jaccard": jacc,
            "gate_pass": gate_pass,
            "decision": res.decision,
            "pred": predicted_same,
            "note": note,
        })

    # --- 汇总 ---
    tp = sum(1 for r in rows if r["pred"] and r["label"])
    fp = sum(1 for r in rows if r["pred"] and not r["label"])
    fn = sum(1 for r in rows if not r["pred"] and r["label"])
    tn = sum(1 for r in rows if not r["pred"] and not r["label"])

    print()
    print("=" * 108)
    print("混淆矩阵（正类 = 判定为同一商品）")
    print("=" * 108)
    print(f"  真阳性 TP = {tp}   正确归并（省下重复商品条目）")
    print(f"  真阴性 TN = {tn}   正确分开")
    print(f"  假阳性 FP = {fp}   ★ 错归并 —— 会让价格序列混进别的商品，分析结论静默失真")
    print(f"  假阴性 FN = {fn}   漏归并 —— 只是少几个价格点（下落分两种，见代价清单）")

    # 同款里没被自动归并的：三层配合与代价清单都要用，先算好，免得两处各算一遍。
    missed_all = [r for r in rows if r["label"] and not r["pred"]]
    n_silent_all = sum(1 for r in missed_all if r["decision"] is Decision.NEW)

    print()
    print("=" * 108)
    print("分层归因：危险的那几对是被谁拦下的？")
    print("=" * 108)
    for r in rows:
        if not r["label"]:
            who = "硬门槛（型号 token）" if not r["gate_pass"] else \
                  f"相似度（{r['score']:.3f} < {MERGE_THRESHOLD}）" if not r["pred"] else \
                  "★ 没人拦下"
            print(f"  {r['note'][:40]:<42} → {who}")

    # --- 核心结论 ---
    print()
    print("=" * 108)
    print("★ 核心结论：三种相似度指标全部【不可分】；")
    print("★ 硬门槛把误判上界压下来，但**两类仍然重叠**——")
    print("★ 零错归并靠的是「门槛 + 阈值 + 灰色地带」三层配合，不是任何单层。")
    print("=" * 108)
    print(f"{'指标':<10} {'真阳性最低':>10} {'假阳性最高':>10}   判定（全部 %d 对）" % len(rows))
    print("-" * 108)
    for name, key in [("包含度", "score"), ("Dice", "dice"), ("Jaccard", "jaccard")]:
        pos = [r[key] for r in rows if r["label"]]
        neg = [r[key] for r in rows if not r["label"]]
        verdict = "可分 ✅" if min(pos) > max(neg) else "★ 不可分 ❌"
        print(f"{name:<10} {min(pos):>10.3f} {max(neg):>10.3f}   {verdict}")

    print()
    print(f"{'--- 加上硬门槛后（只看通过型号门槛的 %d 对）---':<50}"
          % sum(1 for r in rows if r["gate_pass"]))
    gated = [r for r in rows if r["gate_pass"]]
    need_reject = [r["score"] for r in gated if not r["label"]]
    need_accept = [r["score"] for r in gated if r["label"]]
    separable = min(need_accept) > max(need_reject)
    print(f"{'包含度':<10} {min(need_accept):>10.3f} {max(need_reject):>10.3f}   "
          f"{'可分 ✅' if separable else '★ 不可分 ❌'}")
    print()
    worst_fp = max(r["score"] for r in rows if not r["label"])
    print(f"  → 只用相似度：需要接受的最低 {min(need_accept):.3f}，"
          f"需要拒绝的最高 {worst_fp:.3f} —— 任何单一阈值都分不开两类。")
    print(f"  → 加上硬门槛：需要拒绝的最高分从 {worst_fp:.3f} 压到 "
          f"{max(need_reject):.3f}（最危险的假阳性被拦在相似度判定之外）。")
    if separable:
        print(f"    两类可分，阈值 {MERGE_THRESHOLD} 落在 "
              f"[{max(need_reject):.3f}, {min(need_accept):.3f}] 内。")
    else:
        # 小样本上曾"可分"（11 对时 0.500/0.750），123 对上是重叠的——如实报出，
        # 并说清零错归并到底靠什么。
        print(f"    ★ 但要诚实：**两类仍然重叠**（同款最低 {min(need_accept):.3f}，"
              f"不同最高 {max(need_reject):.3f}）——")
        print(f"      '加上硬门槛就立刻可分'在 {len(rows)} 对上**不成立**，"
              f"它只在那 11 对上成立（小样本巧合）。")
        print(f"      零错归并靠的是三层配合，缺一层都会出问题：")
        print(f"        ① 硬门槛   —— 把最危险的假阳性（{worst_fp:.3f}）拦在相似度之外；")
        print(f"        ② 归并阈值 —— {MERGE_THRESHOLD} 高于通过门槛的假阳性上限 "
              f"{max(need_reject):.3f}（裕度 {MERGE_THRESHOLD - max(need_reject):.3f}）；")
        print(f"        ③ 灰色地带 —— 漏网的 {len(missed_all)} 对里 "
              f"{len(missed_all) - n_silent_all} 对被标进人工队列，"
              f"其余 {n_silent_all} 对静默漏掉（见下方代价清单）。")

    # 拦下的高分对里既有真·拦截（不同商品）也有保守代价（同款）——两者混在一张
    # 清单里会让人误读，所以分开报。
    print()
    print("=" * 108)
    print(f"硬门槛拦下、且包含度 >= {MERGE_THRESHOLD} 的对：拦对了还是拦错了？")
    print("=" * 108)
    at_risk = [r for r in rows if not r["gate_pass"] and r["score"] >= MERGE_THRESHOLD]
    for r in at_risk:
        verdict = "拦对了 —— 不同商品，单靠相似度会【错误归并】" if not r["label"] else \
                  "拦错了 —— 同款，被保守策略挡下（进人工队列）"
        print(f"  包含度 {r['score']:.3f} → {verdict}")
        print(f"    {r['note']}")
    n_saved = sum(1 for r in at_risk if not r["label"])
    if at_risk:
        print()
        print(f"  合计 {len(at_risk)} 对：拦对 {n_saved} 对（避免错误归并），"
              f"拦错 {len(at_risk) - n_saved} 对（保守的代价）。")
    else:
        print("  （无）")

    print()
    print("=" * 108)
    print("没有被自动归并的同款 —— 本次的代价清单（含两种下落）")
    print("=" * 108)
    missed = missed_all
    for r in missed:
        who = "硬门槛拦下" if not r["gate_pass"] else (
            "落在灰色地带" if r["score"] >= GREY_ZONE_THRESHOLD else "分数低于灰色地带")
        if r["decision"] is Decision.MARK:
            fate = "标进人工队列（还能被发现）"
        else:
            fate = "★ 标 new —— 静默漏掉，没有任何提示"
        print(f"  包含度 {r['score']:.3f}  {who} → {fate}")
        print(f"    {r['note']}")
    n_silent = n_silent_all
    print()
    print(f"  代价合计：{len(missed)} 对同款不自动归并 —— "
          f"{len(missed) - n_silent} 对进了人工队列，"
          f"{n_silent} 对连标记都没有（彻底漏掉）。")
    fp_risk = sum(1 for r in rows if not r["label"] and r["score"] >= MERGE_THRESHOLD)
    print(f"  对照：若放宽成「子集相容」，会放进 {fp_risk} 对【不同商品】——"
          f"那属于静默污染价格序列，比漏归并严重得多。")


if __name__ == "__main__":
    evaluate(load_titles())
