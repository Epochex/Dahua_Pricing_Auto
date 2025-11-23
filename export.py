import os

def export_py_files(root_dir, output_file="code_description.txt"):
    with open(output_file, "w", encoding="utf-8") as out:
        for dirpath, _, filenames in os.walk(root_dir):
            for filename in filenames:
                if filename.endswith(".py"):
                    file_path = os.path.join(dirpath, filename)

                    out.write("=" * 80 + "\n")
                    out.write(f"FILE: {file_path}\n")
                    out.write("=" * 80 + "\n\n")

                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            content = f.read()
                    except UnicodeDecodeError:
                        # 回退到 latin-1 防止非 UTF-8 文件直接 crash
                        with open(file_path, "r", encoding="latin-1") as f:
                            content = f.read()

                    out.write(content)
                    out.write("\n\n")

    print(f"Done. All .py files exported to {output_file}")


if __name__ == "__main__":
    # 修改为你要扫描的目录，例如当前目录 "."
    export_py_files(".")
