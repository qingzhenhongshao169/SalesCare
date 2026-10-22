"""Small-corpus lexical retrieval; no embedding credentials required."""
import math
import re
from collections import Counter


def eligible(d, channel, stamp):
    return [k for k in d["knowledge"] if k["status"] == "published"
            and k["visibility"] == "public" and channel in k["channels"]
            and (not k.get("expires_at") or k["expires_at"] > stamp)]


def tokens(text):
    text = text.lower()
    parts = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text)
    return [t for p in parts for t in ([p] if p.isascii() else [p[i:i+2] for i in range(len(p)-1)])]


def query_hints(text):
    """Business-question synonyms, independent of company names or knowledge IDs."""
    rules=[
        (r'哪个城市|什么地方|地址|在哪里|在哪儿','地址 在哪里',r'在哪里|地址'),
        (r'最低.{0,3}价|\d+(?:\.\d+)?\s*万.{0,4}(?:能做|就能做)|多少钱|报价|价格|费用|预算','报价 价格',r'报价|收费'),
        (r'多久|几天|[一二三四五六七八九十百\d]+天.{0,6}上线|下个?月.{0,6}上线|工期|交付时间','工期 交付 多久',r'交付.*多久|工期'),
        (r'准备|前期|(?:需要|提供).{0,6}(?:资料|材料)|哪些(?:资料|材料)|参考.{0,12}(?:风格|网站)|需求沟通|功能范围|界定功能|旧系统改造','准备资料 需求文档 功能清单',r'准备.*资料|合作前'),
        (r'合作|退款|合同','合作 合同 条件',r'合作前'),
        (r'怎么判断|是否适合我们|选择服务商','选择服务商 核对',r'选择服务商'),
        (r'哪些类型.{0,5}企业|服务范围|主要做什么','业务范围 服务总览',r'提供哪些服务'),
        (r'品牌展示|网页设计|网站风格','定制网站 网站建设',r'定制网站|官网建设'),
        (r'制造.{0,4}企业|制造业','制造业 产品 内容',r'制造业企业'),
        (r'ai搜索|生成式搜索','GEO 品牌优化',r'开始布局.*GEO|GEO.*覆盖'),
        (r'软件|系统改造','程序开发 定制开发 管理系统',r'程序定制开发|管理系统'),
    ]
    return [(words,title) for pattern,words,title in rules if re.search(pattern,text,re.I)]


class LexicalScorer:
    """Two-pass BM25 statistics bounded by query terms, shared with legacy retrieval."""
    def __init__(self, text):
        self.text = text
        self.hints = query_hints(text)
        self.query = set(tokens(text+' '+' '.join(words for words,_ in self.hints)))
        self.frequencies = Counter()
        self.count = 0
        self.total = 0

    @staticmethod
    def document(k):
        return Counter(tokens(k['title']+' '+' '.join(k['keywords'])+' '+k['content']))

    def observe(self, k):
        doc = self.document(k)
        self.count += 1
        self.total += sum(doc.values())
        self.frequencies.update(set(doc) & self.query)

    @property
    def average(self):
        return self.total/max(1, self.count)

    def score(self, k):
        text, hints, query = self.text, self.hints, self.query
        doc = self.document(k)
        n = sum(doc.values())
        score = sum(math.log(1+(self.count-self.frequencies[t]+.5)/(self.frequencies[t]+.5)) *
                    doc[t]*2.2/(doc[t]+1.2*(.25+.75*n/max(1, self.average))) for t in query if doc[t])
        hits = [word for word in k["keywords"] if word.lower() in text.lower()]
        generic = {"geo", "seo", "网站", "官网", "是什么", "区别", "济南", "增长", "内容", "功能"}
        score += sum((1 if word.lower() in generic else 2+min(len(word), 7)) for word in hits)
        score += len(set(tokens(k["title"])) & query)*1.5
        # General SEO/GEO/website tokens alone shouldn't overpower a specific question.
        meaningful = [w for w in hits if w.lower() not in generic]
        if meaningful: score += 4
        for _,pattern in hints:
            if re.search(pattern,k['title'],re.I):score+=18
        # Penalize unrelated topic pages rather than forcing a company-specific answer.
        website=bool(re.search(r'官网|网站|网页|建站',text))
        geo=bool(re.search(r'GEO|SEO|AI搜索|生成式',text,re.I))
        software=bool(re.search(r'软件|小程序|CRM|ERP|系统',text,re.I))
        if website and not geo and re.search(r'GEO|制造业|采购',k['title'],re.I):score-=16
        if software and not geo and k.get('category')=='程序开发':score+=10
        if re.search(r"多少钱|价格|报价|费用|预算|收费",text) and "报价" in k["title"]: score += 28
        if re.search(r"维护|续费|年费",text) and "如何收费" in k["title"]: score += 24
        if "geo" in text.lower() and "seo" in text.lower() and "区别" in k["title"] and "GEO" in k["title"]: score += 35
        if "geo" in text.lower() and re.search(r"什么是|是什么",text) and "GEO 是什么" in k["title"]: score += 20
        # Preparation questions need the materials FAQ ahead of broad website/GEO pages.
        if re.search(r"准备.{0,10}(?:资料|材料|什么)|(?:资料|材料).{0,8}准备",text) and re.search(r"准备.{0,8}(?:资料|材料)",k["title"]): score += 32
        return score if score >= 6 and (hits or len(set(doc)&query) >= 3) else None


def retrieve(d, channel, text, stamp, limit=4):
    if re.search(r"天气|下雨|气温|足球|彩票|股票代码", text):
        return []
    records = eligible(d, channel, stamp)
    scorer = LexicalScorer(text)
    for record in records:
        scorer.observe(record)
    scores = [(score, k) for k in records if (score := scorer.score(k)) is not None]
    scores.sort(key=lambda x:x[0], reverse=True)
    return [k for _, k in scores[:limit]]


def citation(k):
    return {"id":k["id"], "title":k["title"], "version":k["version"], "url":k.get("source_url"),
            **({'location':k['source_location']} if k.get('source_location') else {}),
            **({'evidence':k['evidence']} if k.get('evidence') else {})}
