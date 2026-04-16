import glob, os

html_files = glob.glob('static/*.html')
for fpath in html_files:
    if fpath.endswith("app.html"): continue
    
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Replace the interceptor target
    old_target = "const a = e.target.closest('a.nav-a');"
    new_target = "const a = e.target.closest('a[href^=\"/\"]');"
    
    if old_target in content:
        content = content.replace(old_target, new_target)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Fixed interceptor in {fpath}")

print("Interceptor fix complete.")
