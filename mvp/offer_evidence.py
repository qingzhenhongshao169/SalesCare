"""Conservative validation of flat public CNY prices against canonical sources."""
import re
from decimal import Decimal, InvalidOperation

MONEY=re.compile(r'(?<![\d.,])(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万元|千元|元)(?![\d])')
CONDITIONAL=re.compile(r'仅限|老客户|新客户|续约|优惠|折扣|不含|另计|另行收费|限时|有效期|截至|含税|未税|如果|假设|假如|参考价|原价|历史价|美元|港元|日元|欧元|USD|HKD|JPY|EUR|[$€]',re.I)


def supports_public_price(record,name,price):
    """Ambiguous/conditional prices require a reviewed explicit pricing entry.

    The existing offer UI has no currency, billing period or condition fields,
    so do not project such values as unconditional flat prices.
    """
    if not record or not name:return False
    if price is None:return True
    try:expected=Decimal(str(price))
    except (InvalidOperation,ValueError):return False
    if not expected.is_finite() or expected<0:return False
    content=record['content']
    if CONDITIONAL.search(content):return False
    subject=re.sub(r'\s+','',name).casefold()
    if not subject:return False
    for clause in re.split(r'[。！？!?；;，\n]|(?<!\d),(?!\d)',content):
        if subject not in re.sub(r'\s+','',clause).casefold():continue
        for match in MONEY.finditer(clause):
            before=clause[:match.start()];after=clause[match.end():]
            labels=list(re.finditer(r'公开价格|价格|售价|报价|费用|收费|定价|公开价|退款|预算|押金|定金|赔偿|优惠|减免|节省',before))
            if not labels or labels[-1][0] in ('退款','预算','押金','定金','赔偿','优惠','减免','节省'):continue
            label=labels[-1]
            if not re.fullmatch(r'[\s:：¥￥]*(?:(?:是|为|人民币)[\s:：¥￥]*)*',before[label.end():]):continue
            head=re.sub(r'\s+','',before[:label.start()]).casefold()
            subject_at=head.rfind(subject)
            if subject_at<0 or not re.fullmatch(r'(?:(?:的|公开|基础|标准|统一|服务))*',head[subject_at+len(subject):]):continue
            if re.match(r'\s*(?:每|/|起|至|到|[-~～])',after):continue
            amount=Decimal(match['number'].replace(',',''))*{'元':1,'千元':1000,'万元':10000}[match['unit']]
            if amount==expected:return True
    return False


def current_offer_records(session,tid,ids,channel_id=None):
    from .knowledge_retrieval import PostgresScan
    return {r['id']:r for r,_,_ in PostgresScan(session,tid,channel_id).scan(ids=list(ids))} if ids else {}
