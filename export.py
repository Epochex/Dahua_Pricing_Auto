#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os

def export_project_code(output_file="code_description.txt"):
    # 1) 只递归这些目录（你按自己项目结构增删）
    include_roots = [
        "frontend/src",
        "backend",
        "core",
        "services",
        "deploy",
    ]

    # 2) 还要额外导出的“单文件”（不在 include_roots 下的）
    include_files = [
        "frontend/index.html",
        "frontend/vite.config.js",
        "frontend/package.json",
        "frontend/package-lock.json",
        "README.md",
        ".env.example",
    ]

    # 3) 只导出这些后缀
    suffixes = (".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".html")

    # 4) 全局排除目录（即便它们出现在 include_roots 里也不下钻）
    exclude_dirs = {
        ".git", ".venv", "venv", "env",
        "__pycache__", ".pytest_cache", ".mypy_cache",
        "node_modules",
        "dist", "build", "out",
        ".next", ".nuxt",
        ".cache", ".idea", ".vscode",
        "coverage",
        "runtime", "data", "logs", "tmp",
    }

    # 5) 防止把大产物扫进去（可调）
    max_bytes = 2 * 1024 * 1024  # 2MB

    def write_one(fp: str, out):
        try:
            if os.path.getsize(fp) > max_bytes:
                return
        except OSError:
            return

        out.write(f"FILE: {fp}\n")
        try:
            with open(fp, "r", encoding="utf-8") as f:
                out.write(f.read())
        except UnicodeDecodeError:
            with open(fp, "r", encoding="latin-1") as f:
                out.write(f.read())
        except OSError:
            return
        out.write("\n\n")

    with open(output_file, "w", encoding="utf-8") as out:
        # 递归导出：只在 include_roots 内走
        for root in include_roots:
            root = os.path.normpath(root)
            if not os.path.isdir(root):
                continue

            for dirpath, dirnames, filenames in os.walk(root):
                # 关键：阻止进入排除目录
                dirnames[:] = [d for d in dirnames if d not in exclude_dirs]

                for fn in filenames:
                    if not fn.endswith(suffixes):
                        continue
                    fp = os.path.join(dirpath, fn)
                    write_one(fp, out)

        # 额外导出：单文件白名单
        for rel in include_files:
            fp = os.path.normpath(rel)
            if os.path.isfile(fp):
                write_one(fp, out)

    print(f"Done. Exported project code to {output_file}")

if __name__ == "__main__":
    export_project_code()
