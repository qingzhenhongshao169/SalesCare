"""Source-backed trial corpus; paraphrased policies remain unapproved candidates."""
import json,re,sys,hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import httpx
from lxml import html
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'knowledge/hisense-trial-20261013';FILES=OUT/'知识文档';FILES.mkdir(parents=True,exist_ok=True)
sources=json.loads((ROOT/'.mvp-runtime/hisense-source-review/sources.json').read_text('utf-8'))
def clean(s):return re.sub(r'\s+',' ',s).strip()
def enrich(p):
    r=httpx.get(p['url'],timeout=25,follow_redirects=True)
    if r.status_code!=200 or r.url.host!='mall.hisense.com':raise RuntimeError('Unexpected source')
    d=html.fromstring(r.text);pairs=[];budget=0
    for e in d.xpath('//div[@class="attr-group-item"]'):
        group=clean(' '.join(e.getparent().getparent().xpath('./div[contains(@class,"attr-group-title")]//text()')))
        k=clean(' '.join(e.xpath('./span[contains(@class,"title")]//text()'))).rstrip(': ：')
        v=clean(' '.join(e.xpath('./span[contains(@class,"value")]//text()')))
        if not k or not v or any(x in k for x in ['上市','重量','CPU','底座材质','边框材质','质量']):continue
        label=(group+' / '+k) if group else k
        if budget+len(label+v)>360:continue
        pairs.append([label,v]);budget+=len(label+v)
        if len(pairs)>=12:break
    model=re.search(r'KFR-[A-Za-z0-9/+-]+|BCD-[A-Z0-9]+|BD(?:/BC)?-[A-Z0-9]+|(?:WF|WD|WT|DH|ES|WQ)[A-Za-z0-9+.-]+|JZT-[A-Z0-9]+|(?:55|75|85)[A-Z0-9-]+',p['title'])
    identifier=model[0] if model else p['url'].rsplit('/',1)[-1]
    if identifier in ('BCD-509E5SFZSD',):identifier+='白' if '白' in p['title'] else '灰'
    brand='科龙' if '科龙' in p['title'] else 'Vidda' if 'Vidda' in p['title'] else '海信'
    return {**p,'model':identifier,'brand':brand,'parameters':pairs}
policies={
 '全国联保':'官方商城的全国联保文章引导用户通过“海信爱家”公众号或售后热线4006-111-111查询服务网点、三包及收费。它不是某型号完整保修期限清单，不能推出所有部件统一保修年数。咨询时先核对型号、购买渠道、购买或签收时间及故障现象。',
 '七天无理由':'官网文章介绍商城七天无理由申请路径、签收次日起计算及商品完好条件，并涉及配件、赠品与运费。其页面日期为2019年，试验时仅作为待复核资料；不要直接认定某笔订单一定适用或自动批准退款。具体订单应由商城当前规则和工作人员核验。',
 '质量问题退换':'这篇2019年的商城文章介绍质量问题退换流程：联系售后鉴定，再到商城对应订单处理。它包含时间与处理方式，应重新确认现行政策后才能对客发布。不能把页面概述解释为本系统已完成售后鉴定、一定能退款或已经成功换机。',
 '退款说明':'官网商城文章按订单状态介绍退款入口：未发货从取消订单操作，已发货使用退款中的退货路径。本文是2019年页面的摘要，现行入口应核实。真正的订单状态、支付信息和退款结果必须来自授权订单接口或人工核验，知识库不能凭订单号生成结果。',
 '送装一体':'官方文章标题询问送装一体，但正文描述送装分离，并指向“海信爱家”公众号或安装电话4008-011-111预约。这是标题与正文需要核对的例子，不能仅按标题回答已包含安装。渠道、机型、地区和下单约定可能不同，发布前应核对现行说明。',
 '结算金额核对':'官方商城文章说明页面展示与结算金额可能因缓存、更新延迟等不一致，要求以实际下单付款页面为准。静态知识库不保存即时售价、补贴资格、库存或可用优惠券。客服可以指向商品页面与结算核对入口，涉及价保与金额争议时交由人工处理。',
 '隐私资料':'商城隐私政策涉及购物、物流与售后场景下的信息处理、必要共享、保护以及用户联系渠道。本摘要只用于识别信息治理主题，不替代完整政策或企业授权。订单、地址、电话与合同应按用户身份和权限访问，不能上传为所有访客可召回的公开知识。',
}
manifest=[]
def save(index,title,body,url,kind,scope='public'):
    name=f'{index:02d}-{re.sub(r"[\\/:*?\"<>|]","-",title)}.md'
    text=f'# {title}\n\n## 来源与范围\n\n来源：{url}\n\n采集日期：2026-10-13。市场：中国大陆。用途：非官方试验资料，供企业客服流程验证。\n\n'+body
    (FILES/name).write_text(text,'utf-8')
    manifest.append({'file':name,'title':title,'source_url':url,'region':'CN','captured_at':'2026-10-13','kind':kind,'visibility':scope,'sha256':hashlib.sha256(text.encode()).hexdigest(),'characters':len(text)})
if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    with ThreadPoolExecutor(3) as pool:products=list(pool.map(enrich,sources['products']))
    assert len({p['url'] for p in products})==30
    for i,p in enumerate(products,1):
        table='\n'.join('| '+k.replace('|','/')+' | '+v.replace('|','/')+' |' for k,v in p['parameters'])
        body=f'## 商品身份\n\n品牌：{p["brand"]}。品类：{p["category"]}。商品型号：{p["model"]}。\n\n## 已核对的页面参数\n\n下表来自此商品的文字参数区，分组与单位保留。\n\n| 参数与页面分组 | 页面记录值 |\n| --- | --- |\n{table}\n\n## 咨询使用\n\n先确认客户问的是 {p["model"]}，再引用本型号对应字段。不同规格和不同商品不能合并参数；没有列出的项目需查看对应说明书或由工作人员核实。\n\n## 适用边界\n\n资料仅覆盖文字参数，不覆盖图片中未读取的内容，不代表实时交易数据。选型可继续询问使用环境、安装位置和用户关注的功能，勿把用户偏好写成产品功能。\n'
        save(i,p['brand']+'-'+p['category']+'-'+p['model'],body,p['url'],'product')
    for i,a in enumerate(sources['articles'],31):
        save(i,'待复核-'+a['label'],'## 页面摘要\n\n'+policies[a['label']]+'\n\n## 发布要求\n\n保留为企业内部候选资料。核对页面更新时间、销售渠道和现行适用范围，并由负责人复核后再决定是否对客使用。\n',a['url'],'policy','internal')
    save(38,'海信-官网产业与家电入口','## 官方入口摘要\n\n集团官网展示电视、空调、冰箱、洗衣机、冷柜冰吧和厨卫等家电入口，也展示半导体、智慧能源、激光产业、汽车电子与内容平台等领域。此资料用于识别咨询类别，不意味着这些领域共享产品参数或售后规则。\n\n## 企业与消费者咨询\n\n消费者可先说明品类、型号及问题；企业咨询可说明业务场景和项目范围，再联系对应人员。SalesCare中的海信试验空间是非官方测试，不接收真实海信订单、不代表海信履行服务。\n','https://www.hisense.com/','company')
    (OUT/'manifest.json').write_text(json.dumps({'count':38,'documents':manifest,'products':products},ensure_ascii=False,indent=2),'utf-8')
    print('Created 38 source-backed documents:',FILES,'characters:',sum(x['characters'] for x in manifest))
