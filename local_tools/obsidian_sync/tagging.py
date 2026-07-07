from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class TagRule:
    keywords: Sequence[str]
    weight: int


DEFAULT_TAGGING_RULES: Dict[str, TagRule] = {
    "电商/Amazon": TagRule(["Amazon", "亚马逊", "FBA", "ASIN", "listing", "Buy Box", "选品"], 10),
    "电商/独立站": TagRule(["独立站", "shopify", "建站", "landing page", "DTC"], 8),
    "电商/选品": TagRule(["选品", "调研", "niche", "竞品", "产品开发"], 10),
    "电商/TikTok-Shop": TagRule(["TikTok Shop", "抖音电商", "小店", "抖音带货"], 8),
    "营销/Facebook": TagRule(["Facebook", "FB广告", "脸书", "Meta广告"], 8),
    "营销/Instagram": TagRule(["Instagram", "ins", "IG", "reels", "快拍"], 8),
    "营销/小红书": TagRule(["小红书", "xhs", "种草", "笔记"], 8),
    "营销/SEO": TagRule(["SEO", "搜索引擎", "排名", "关键词", "外链", "backlink"], 10),
    "商业/创业方法论": TagRule(["创业", "从0到1", "商业计划", "MVP", "白手起家", "创业起步"], 10),
    "商业/产业分析": TagRule(["产业", "行业", "赛道", "市场规模", "商业模式"], 8),
    "商业/企业管理": TagRule(["团队", "招聘", "合伙", "组织架构", "企业文化"], 8),
    "商业/成本效率": TagRule(["降本增效", "成本控制", "效率提升", "利润"], 8),
    "商业/长期主义": TagRule(["长期主义", "段永平", "本分", "慢就是快", "价值投资"], 10),
    "投资/价值投资": TagRule(["巴菲特", "芒格", "价值投资", "复利", "长期持有"], 10),
    "投资/宏观经济": TagRule(["GDP", "通胀", "利率", "美联储", "央行", "经济周期"], 8),
    "投资/股票": TagRule(["A股", "美股", "港股", "大盘", "牛市", "熊市", "股票"], 8),
    "投资/财商教育": TagRule(["财商", "理财", "财务自由", "被动收入", "消费观"], 8),
    "AI/工具应用": TagRule(["ChatGPT", "Claude", "GPT", "Gemini", "Kimi", "大模型", "AI工具"], 10),
    "AI/内容生成": TagRule(["AI写作", "AI生成", "AI视频", "AI图片", "Midjourney", "Sora"], 8),
    "AI/商业应用": TagRule(["AI+", "AI赋能", "AI应用", "商业AI"], 8),
    "技术/开源项目": TagRule(["开源", "GitHub", "Star", "开源项目", "开源社区"], 10),
    "技术/开发工具": TagRule(["IDE", "终端", "CLI", "开发环境", "开发框架"], 8),
    "技术/硬件产品": TagRule(["硬件", "手机", "电脑", "消费电子", "产品评测", "数码"], 8),
    "成长/认知升级": TagRule(["认知升级", "认知突破", "思维模型", "元认知"], 10),
    "成长/心理学": TagRule(["心理学", "进化心理学", "行为经济学", "人性"], 8),
    "成长/时间管理": TagRule(["时间管理", "效能", "习惯", "早起", "自律", "习惯养成"], 8),
    "成长/情绪管理": TagRule(["情绪管理", "焦虑", "压力", "精神内耗", "心理健康"], 8),
    "成长/读书笔记": TagRule(["读书", "书评", "推荐书", "阅读", "书单"], 8),
    "健康/营养学": TagRule(["营养", "维生素", "蛋白质", "碳水", "膳食", "补剂"], 10),
    "健康/力量训练": TagRule(["撸铁", "深蹲", "卧推", "硬拉", "增肌", "力量训练"], 8),
    "健康/体脂管理": TagRule(["减脂", "体脂", "内脏脂肪", "减肥", "热量缺口"], 8),
    "健康/睡眠": TagRule(["睡眠", "失眠", "褪黑素", "昼夜节律", "睡眠质量"], 8),
    "政治/地缘政治": TagRule(["地缘政治", "领土争端", "大国博弈", "战争局势"], 10),
    "政治/军事分析": TagRule(["军事", "战争", "武器", "军队", "核武器", "军阀混战"], 10),
    "政治/时事评论": TagRule(["时事", "热点", "舆论", "社会现象", "热门事件"], 8),
    "自媒体/个人IP": TagRule(["个人IP", "IP打造", "人设", "个人品牌", "IP定位"], 10),
    "自媒体/变现": TagRule(["变现", "知识付费", "带货", "课程", "付费社群"], 8),
    "自媒体/短视频": TagRule(["短视频", "拍摄", "剪辑", "视频制作", "竖屏"], 8),
    "自媒体/播客": TagRule(["播客", "podcast", "音频节目", "访谈", "对话"], 8),
    "生活/社会观察": TagRule(["社会观察", "社会现象", "世相", "众生相"], 8),
    "生活/日常感悟": TagRule(["感悟", "碎碎念", "随笔", "日常", "心情"], 6),
    "生活/美食": TagRule(["美食", "做饭", "餐饮", "食物", "烹饪"], 6),
    "生活/旅行见闻": TagRule(["旅行", "旅游", "观光", "景点", "出行"], 6),
    "副业/信息差": TagRule(["信息差", "套利", "低买高卖", "倒爷", "信息不对称"], 10),
    "副业/自由职业": TagRule(["自由职业", "freelance", "upwork", "远程工作"], 8),
    "副业/出海机会": TagRule(["出海", "海外市场", "全球化", "出海创业"], 8),
}


DEFAULT_FALLBACK_TAGS = {
    "weibo": "电商/运营实操",
    "x": "生活/社会观察",
    "wechat": "商业/产业分析",
    "douyin": "成长/认知升级",
    "xiaoyuzhou": "成长/心理学",
    "xiaohongshu": "营销/小红书",
}


ASCII_WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def keyword_hit(text: str, keyword: str) -> bool:
    keyword = str(keyword or "").strip()
    if not keyword:
        return False
    normalized_keyword = keyword.lower()
    if ASCII_WORD_RE.fullmatch(keyword):
        pattern = rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return normalized_keyword in text


def section_score(text: str, rule: TagRule, multiplier: int) -> int:
    normalized_text = normalize_text(text)
    if not normalized_text:
        return 0
    hits = sum(1 for keyword in rule.keywords if keyword_hit(normalized_text, keyword))
    return hits * int(rule.weight) * multiplier


def score_tags(
    *,
    title: str,
    summary: str,
    body: str,
    rules: Dict[str, TagRule] = DEFAULT_TAGGING_RULES,
) -> List[Tuple[str, int]]:
    body_excerpt = (body or "")[:2000]
    scored: List[Tuple[str, int]] = []
    for tag, rule in rules.items():
        score = 0
        score += section_score(title, rule, 3)
        score += section_score(summary, rule, 2)
        score += section_score(body_excerpt, rule, 1)
        if score >= 2:
            scored.append((tag, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def unique_tags(tags: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for tag in tags:
        clean = str(tag or "").strip().lstrip("#")
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def assign_tags(
    *,
    platform: str,
    title: str,
    summary: str,
    body: str,
    max_tags: int = 5,
    fallback_tags: Dict[str, str] = DEFAULT_FALLBACK_TAGS,
) -> List[str]:
    scored = score_tags(title=title, summary=summary, body=body)
    tags = unique_tags(tag for tag, _score in scored[:max_tags])
    if tags:
        return tags
    fallback = fallback_tags.get(str(platform or "").strip().lower(), "成长/认知升级")
    return [fallback]
