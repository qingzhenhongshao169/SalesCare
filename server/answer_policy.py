"""Keep customer requirements distinct from supplier promises in numeric checks.

This is a conservative local guard, not a semantic verifier. Never treat user
numbers as permission to quote the same price or promise the same deadline.
"""
from decimal import Decimal
import re

TOKEN = re.compile(r'(?P<number>\d[\d,]*(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)(?P<unit>\s*(?:万元|千元|元|万|千|个月|星期|周|天|月|个(?:独立设计的|独立|单独)?页面|页面|页|人|个|条|项|%|％))?')
CHINESE = {'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}

def quantity(match):
    raw=match['number'].replace(',',''); unit=(match['unit'] or '').strip()
    if not raw[0].isdigit():
        if not unit:return None  # Ordinary words such as “一项” aren't quantities.
        total=section=value=0
        for ch in raw:
            if ch in CHINESE:value=CHINESE[ch]
            elif ch=='万':total+=(section+value)*10000;section=value=0
            else:section+=(value or 1)*{'十':10,'百':100,'千':1000}[ch];value=0
        amount=Decimal(total+section+value)
    else:amount=Decimal(raw)
    multiplier={'万元':10000,'万':10000,'千元':1000,'千':1000}.get(unit,1)
    dimension=('money' if unit in ('万元','万','千元','千','元') else
               'week' if unit in ('周','星期') else 'page' if '页面' in unit or unit=='页' else
               'month' if unit in ('月','个月') else unit)
    return amount*multiplier,dimension

def numbers_supported(answer,evidence,history):
    # A line-leading list marker is formatting, never a supported price/count.
    answer=re.sub(r'(?m)^\s*\d{1,2}[.、)]\s*(?!\d)', '', answer)
    customer_text='\n'.join(m['content'] for m in history if m['role']=='user')
    known={quantity(m) for m in TOKEN.finditer(evidence)}-{None}
    customer={quantity(m) for m in TOKEN.finditer(customer_text)}-{None}
    clauses=[(sentence,clause) for sentence in re.split(r'[。；;！!\n]',answer)
             for clause in re.split(r'[，]|(?<!\d),|,(?!\d)',sentence)]
    for sentence,clause in clauses:
        owned=bool(re.search(r'您|你|客户|用户|对方',sentence))
        clarification=bool(re.search(r'是指|指的是|独立设计|内容条数|还是',clause) and re.search(r'[？?]',answer))
        for match in TOKEN.finditer(clause):
            value=quantity(match)
            if value is None or value in known:continue
            before=clause[max(0,match.start()-18):match.start()]
            after=clause[match.end():match.end()+18].lstrip()
            promise=bool(re.search(r'(?:报价|收费|售价|起价|价格|工期|交付周期|仅需|只需|只要|支持|提供|保证|承诺)(?:是|为|仅|约|大概|在|最多|最少|不超过|人民币|\s)*$',before)
                         or re.search(r'^(?:以内|之内|内)?(?:就能|就可以|足够|够用|包做|可做|能做|可以做|交付|完成)',after)
                         or re.search(r'(?:能|可|保证|承诺)(?:在|以|用)?\s*$',before))
            attributed=owned or bool(re.search(r'预算|希望|期望|提到|说的',before+after))
            if value in customer and (attributed or clarification) and not promise:continue
            # Clarifying “pages” versus “content items” may use different count
            # words. Never convert a customer count into money or a deadline.
            count_context=clarification or bool(re.search(r'页面|内容|产品|条数|独立设计',clause))
            if count_context and not promise and value[1] in ('page','个','条','项',''):
                if any(n==value[0] and unit in ('page','个','条','项','') for n,unit in customer):continue
            return False
    return True


def date_ranges_supported(answer,records):
    """A date pair must occur together in one source, not be spliced across sources."""
    pattern=r'(\d{1,2})月(\d{1,2})日\s*[至到—-]\s*(?:(\d{1,2})月)?(\d{1,2})日'
    def values(text):return {(a,b,c or a,d) for a,b,c,d in re.findall(pattern,text)}
    claimed=values(answer)
    if not claimed:return True
    supported=set().union(*(values(k['content']) for k in records)) if records else set()
    return claimed<=supported

def ungrounded_company_claim(answer,company):
    identity=r'(?:我们|我司|本公司|本企业|'+re.escape(company)+r')'
    return bool(re.search(identity+r'.{0,14}(?:提供|支持|擅长|拥有|成立|位于|收费|价格|报价|交付|保证|承诺|案例)',answer)
                or re.search(r'(?:价格|报价|费用|收费|工期)(?:是|为|只要|仅需)\s*[\d一二两三四五六七八九十]',answer))
