#!/usr/bin/env python3
"""给前端 HTML 写入后端地址和内容指纹。被 sync-frontend.sh 和 push.sh 调用。

    stamp.py <文件> <API_BASE>   # 输出盖好戳的 HTML
    stamp.py <文件> --build-id   # 只输出该文件应有的指纹

指纹算的是"把两个 meta 清空之后"的内容，所以跟填什么后端地址无关 ——
本地算出来的和线上页面里写的必然一致，可以用来验证部署有没有真的生效。
"""
import hashlib
import re
import sys


def put(text: str, name: str, value: str) -> str:
    out, n = re.subn(f'(<meta name="{name}" content=")[^"]*(">)',
                     lambda m: m.group(1) + value + m.group(2), text, count=1)
    if n != 1:
        sys.exit(f'找不到唯一的 <meta name="{name}">，模板被改坏了：{name}')
    return out


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, arg = sys.argv[1], sys.argv[2]
    html = open(src, encoding="utf-8").read()

    bare = put(put(html, "hivora-api", ""), "hivora-build", "")
    build = hashlib.sha256(bare.encode()).hexdigest()[:12]

    if arg == "--build-id":
        print(build)
        return
    sys.stdout.write(put(put(html, "hivora-api", arg), "hivora-build", build))


if __name__ == "__main__":
    main()
