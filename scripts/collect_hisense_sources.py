"""Fetch only official CN shop pages; never import redirected overseas material."""
import json,re,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import httpx
from lxml import html
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.mvp-runtime/hisense-source-review';OUT.mkdir(exist_ok=True)
BASE='https://mall.hisense.com'
def page(url):
    with httpx.Client(timeout=25,follow_redirects=True) as c:r=c.get(url)
    if r.status_code!=200 or r.url.host!='mall.hisense.com':raise ValueError('Source redirected or unavailable')
    return html.fromstring(r.text)
def clean(s):return re.sub(r'\s+',' ',s).strip()
def category(cat):
    d=page(BASE+'/search?fcid='+str(cat))
    return list(dict.fromkeys(d.xpath('//a[contains(@href,"/items/")]/@href')))
def product(url):
    try:
        d=page(BASE+url);title=clean(' '.join(d.xpath('//h1//text()')))
        pairs=[]
        for e in d.xpath('//div[contains(@class,"attr-group-item")]'):
            k=clean(' '.join(e.xpath('./span[contains(@class,"title")]//text()'))).rstrip(': ：')
            v=clean(' '.join(e.xpath('./span[contains(@class,"value")]//text()')))
            if k and v:pairs.append([k,v])
        return {'url':BASE+url,'title':title,'parameters':pairs}
    except Exception:return None
def article(spec):
    label,query=spec;url=BASE+'/pub/articles/detail?'+query
    d=page(url)
    for e in d.xpath('//script|//style|//header|//footer'):e.getparent().remove(e)
    text='\n'.join(clean(x) for x in d.xpath('//text()') if clean(x))
    (OUT/(query.split('&')[0].replace('=','-')+'.txt')).write_text(text,'utf-8')
    return {'label':label,'url':url,'review_text':text}
if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cats=[('电视',657),('空调',4),('冰箱',411),('洗衣机',6),('冷柜',7),('厨卫',238)]
    with ThreadPoolExecutor(3) as pool:groups=list(pool.map(lambda x:category(x[1]),cats))
    allproducts=[]
    for (label,cat),links in zip(cats,groups):
        with ThreadPoolExecutor(3) as pool:items=list(pool.map(product,links[:12]))
        valid=[{**x,'category':label} for x in items if x and x['parameters'] and re.search(r'海信|Hisense',x['title'],re.I)]
        print(label,'readable products',len(valid))
        allproducts.extend(valid[:5])
    specs=[('全国联保','id=28&menuId=13'),('七天无理由','id=166&menuId=13'),('质量问题退换','id=23&menuId=13'),('退款说明','id=67&menuId=23'),('送装一体','id=101&menuId=19'),('结算金额核对','id=236&menuId=13'),('隐私资料','id=1378')]
    with ThreadPoolExecutor(3) as pool:articles=list(pool.map(article,specs))
    result={'date':'2026-10-13','region':'中国大陆','products':allproducts,'articles':articles}
    (OUT/'sources.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print('Reviewed products:',len(allproducts),'articles:',len(articles))
    for a in articles:print(a['label'],a['review_text'][-5500:])
