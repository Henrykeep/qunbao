"""NapCat 的 QQ 媒体缓存清理（图片不存服务器，缓存只留几小时）。"""
import contextlib, os, time

QQ_CACHE_DIRS = {"Pic", "Video", "Ptt", "Thumb"}


def clean_qq_cache(root=None, max_age=6 * 3600, now=None):
    root = root or os.getenv("NAPCAT_DATA", "/napcat_qq")
    now = now or time.time()
    freed = files = 0
    if not root or not os.path.isdir(root):
        return 0, 0
    for dp, _dns, fns in os.walk(root, topdown=False):
        parts = dp.replace(os.sep, "/").split("/")
        if "nt_data" not in parts or not (QQ_CACHE_DIRS & set(parts[parts.index("nt_data") + 1:])):
            continue
        for fn in fns:
            fp = os.path.join(dp, fn)
            try:
                st = os.lstat(fp)
                if os.path.isfile(fp) and not os.path.islink(fp) and now - st.st_mtime > max_age:
                    os.remove(fp)
                    freed += st.st_size
                    files += 1
            except OSError:
                pass
        if dp.split(os.sep)[-1] not in QQ_CACHE_DIRS:
            with contextlib.suppress(OSError):
                os.rmdir(dp)  # 只删空的月份子目录
    if files:
        print(f"清理 QQ 媒体缓存：{files} 个文件，{freed / 1048576:.1f} MB")
    return files, freed
