#!/usr/bin/env python3
"""
入口：解析命令行 / 交互式输入 PN
简单示例：python main.py --pn 12345
"""
import argparse
import sys
from core import loader, classifier, pricing_engine, formatter
from config import APP_TITLE


def parse_args():
    p = argparse.ArgumentParser(description=APP_TITLE)
    p.add_argument("--pn", help="part number / product number", required=False)
    return p.parse_args()


def interactive_input():
    try:
        return input('Enter PN: ').strip()
    except KeyboardInterrupt:
        print('\nInterrupted')
        sys.exit(1)


def main():
    args = parse_args()
    pn = args.pn or interactive_input()
    print(f"{APP_TITLE} - processing PN={pn}")

    # 1) load data
    data = loader.load_all()

    # 2) classify
    cls = classifier.classify(pn, data)

    # 3) compute prices
    result = pricing_engine.compute_prices(pn, cls, data)

    # 4) format output
    formatter.write_output(result)


if __name__ == '__main__':
    main()
