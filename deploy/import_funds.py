# -*- coding: utf-8 -*-
"""批量把基金加进新站（阿里云 ECS）。

准备：在项目根目录建 funds.txt，一行一个 6 位代码，# 开头是注释：

    # 港股互联网
    013171
    012348

运行（本地 Windows，用项目自带的 venv）：
    venv\\Scripts\\python.exe deploy\\import_funds.py

换地址：
    set FUND_API=http://47.111.231.52:8443
    venv\\Scripts\\python.exe deploy\\import_funds.py

为什么每个间隔 5 秒：东财 fundmobapi 对同一 IP 有频率风控，
连续快速请求会返回 61136403，后面几分钟都拿不到数据。
21 只基金大约需要 2 分钟，让它慢慢跑，别中断。
"""
import os
import sys
import time

import requests

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

API = os.environ.get('FUND_API', 'http://47.111.231.52:8443').rstrip('/')
LIST_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'funds.txt')
INTERVAL = 5


def load_codes(path):
    if not os.path.exists(path):
        sys.exit('找不到 %s，请先建一个，一行一个基金代码' % path)
    codes = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # 允许「代码 名称」这种写法，只取前 6 位数字
            token = line.split()[0]
            if token.isdigit() and len(token) == 6:
                codes.append(token)
            else:
                print('跳过无法识别的行：%s' % line)
    return codes


def main():
    codes = load_codes(LIST_FILE)
    if not codes:
        sys.exit('funds.txt 里没有有效代码')

    print('目标：%s' % API)
    print('待处理 %d 个代码\n' % len(codes))

    try:
        existing = {f['code'] for f in requests.get(API + '/api/funds', timeout=20).json()}
    except Exception as e:
        sys.exit('读不到现有列表，检查服务是否在跑：%s' % e)

    todo = [c for c in codes if c not in existing]
    skipped = len(codes) - len(todo)
    if skipped:
        print('已在列表里，跳过 %d 个\n' % skipped)
    if not todo:
        print('没有需要添加的基金')
        return

    ok, fail = [], []
    for i, code in enumerate(todo, 1):
        try:
            r = requests.post(API + '/api/funds', json={'code': code}, timeout=45)
            data = r.json()
            if r.status_code == 200 and data.get('ok'):
                ok.append(code)
                print('[%d/%d] %s  %s' % (i, len(todo), code, data.get('name', '')))
            else:
                fail.append((code, data.get('error', str(r.status_code))))
                print('[%d/%d] %s  失败：%s' % (i, len(todo), code, data.get('error')))
        except Exception as e:
            fail.append((code, '%s: %s' % (type(e).__name__, str(e)[:80])))
            print('[%d/%d] %s  异常：%s' % (i, len(todo), code, str(e)[:80]))

        if i < len(todo):
            time.sleep(INTERVAL)

    print('\n成功 %d 个，失败 %d 个' % (len(ok), len(fail)))
    if fail:
        print('失败明细：')
        for code, why in fail:
            print('  %s  %s' % (code, why))
        print('\n过几分钟可以重跑本脚本，已成功的会自动跳过')


if __name__ == '__main__':
    main()
