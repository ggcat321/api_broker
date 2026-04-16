import glob, re, os

spa_script = """
<!-- SPA Engine Interceptor -->
<script>
    document.addEventListener('click', e => {
        const a = e.target.closest('a.nav-a');
        if (a && window.parent !== window) {
            e.preventDefault();
            window.parent.postMessage({type:'NAVIGATE', path: a.getAttribute('href')}, '*');
        }
    });
</script>
</body>
"""

html_files = glob.glob('static/*.html')
for fpath in html_files:
    if fpath.endswith("app.html"): continue
    
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 1. Strip target="quant_sub" onclick="..." from nav-a anchors
    content = re.sub(r' target="quant_sub" onclick="window\.open\([^)]+\); return false;"', '', content)
    
    # 2. Inject SPA script before </body> if not present
    if "SPA Engine Interceptor" not in content:
        content = content.replace("</body>", spa_script)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Refactored {fpath}")

print("Done refactoring html files.")
