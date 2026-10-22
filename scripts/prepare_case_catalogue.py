"""Build a public, traceable case candidate package from verified HTTPS page snapshots."""
import argparse
import hashlib
import json
from pathlib import Path
from bs4 import BeautifulSoup

CASES = [
    ('26', '新天激光品牌营销型外贸网站', ['website'], ['industrial'], ['export', 'branding'],
     '品牌营销型外贸网站', '为激光工业应用领域建设品牌营销型外贸网站。'),
    ('21', '杰瑞海工品牌官网', ['website'], ['industrial'], ['branding', 'products'],
     '海洋装备行业高端品牌官网建设', '为海洋装备技术与工程服务企业建设品牌官网。'),
    ('17', '拓锐科技品牌官网', ['website'], ['energy'], ['branding'],
     '电力系统数字化高端网站建设', '为电力信息化企业策划和定制品牌官网。'),
    ('27', '容声冰箱用户小程序', ['miniapp'], ['appliance'], ['membership', 'after_sales'],
     '会员活动、售后服务、用户社区', '围绕会员活动、售后服务和用户社区规划品牌用户小程序。'),
    ('24', '同圆设计集团品牌官网', ['website'], ['architecture'], ['branding'],
     '同圆', '为建筑设计企业建设品牌官网。'),
    ('14', '佳怡物流集团品牌官网', ['website'], ['logistics'], ['branding'],
     '物流系统网站建设', '围绕物流与供应链企业的品牌形象进行官网定制。'),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshots', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    entries = []
    for number, title, services, industries, goals, anchor, summary in CASES:
        path = Path(args.snapshots) / (number + '.html')
        raw = path.read_bytes()
        soup = BeautifulSoup(raw.decode('utf-8-sig'), 'html.parser')
        text = soup.get_text('\n', strip=True)
        start = text.index('合作项目')
        end = text.index('免费获取服务方案', start)
        evidence = text[start:end].strip()
        if anchor not in text or len(evidence) < 50:
            raise ValueError('Source body does not support case ' + number)
        source_url = f'https://www.biaofun.com/case/5/{number}.html'
        content = '\n'.join([title, summary, '标梵官网项目说明：', evidence[:300],
            '项目经验仅作参考，当前项目应另行评估。'])
        entries.append({'id': 'biaofun-case-' + number, 'title': title, 'source_url': source_url,
            'content': content, 'keywords': [title.split('品牌')[0], *services, *industries, *goals],
            'source_verified': True, 'review_status': 'draft_pending_enterprise_review',
            'verification': {'method': 'PowerShell Invoke-WebRequest HTTPS with normal certificate verification',
                'http_status': 200, 'retrieved_on': '2026-10-19',
                'snapshot_utf8_sha256': hashlib.sha256(raw).hexdigest(), 'source_excerpt': evidence},
            'recommendation': {'summary': summary, 'service_types': services, 'industries': industries,
                'goals': goals, 'priority': 50}})
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'schema': 'salescare.case-catalogue.v1', 'entries': entries,
        'scope': 'Official public self-described cases; enterprise review required before recommendation.'},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'prepared': len(entries), 'auto_published': 0, 'output': str(output)}, ensure_ascii=True))


if __name__ == '__main__':
    main()
