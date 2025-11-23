"""输出表格、加粗、| calculated 标签
- 提供 write_output(result) 方法
"""
import json


def write_output(result: dict, out_path: str = 'pricing_result.json') -> None:
    """占位实现：将结果写为 JSON 文件；真实项目可能需要写入 Excel 并标注样式/标签。"""
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'Wrote result to {out_path}')
