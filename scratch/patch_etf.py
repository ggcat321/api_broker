import re

path = '/Users/jeffrey/Downloads/ETF_analyzer.htm'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

helper = """
function matchMulti(q, ...fields) {
  if (!q) return true;
  const terms = String(q).toLowerCase().split(/[\\s,]+/).filter(x => x);
  if (terms.length === 0) return true;
  return terms.some(t => fields.some(f => String(f||"").toLowerCase().includes(t)));
}
"""

if 'function matchMulti' not in text:
    text = text.replace('<script>', '<script>\n' + helper)

# Pattern 1: r.code and r.name
text = re.sub(r'String\(r\.code\)\.toLowerCase\(\)\.includes\(q\) \|\| \s*String\(r\.name\)\.toLowerCase\(\)\.includes\(q\)', r'matchMulti(q, r.code, r.name)', text)

# Pattern 2: r.code and r.en
text = re.sub(r'r\.code\.toLowerCase\(\)\.includes\(q\) \|\| \s*String\(r\.en \|\| ""\)\.toLowerCase\(\)\.includes\(q\)', r'matchMulti(q, r.code, r.en)', text)

# Pattern 3: r["股票代號"] and r["股票名稱"]
text = re.sub(r'String\(r\["股票代號"\]\)\.toLowerCase\(\)\.includes\(q\) \|\| \s*String\(r\["股票名稱"\]\s*\|\|\s*""\)\.toLowerCase\(\)\.includes\(q\)', r'matchMulti(q, r["股票代號"], r["股票名稱"])', text)

# Pattern 4: r["股票代號"] and r["ETF代號"] and r["股票名稱"]
text = re.sub(r'String\(r\["股票代號"\]\)\.toLowerCase\(\)\.includes\(q\) \|\| \s*String\(r\["ETF代號"\]\)\.toLowerCase\(\)\.includes\(q\) \|\| \s*String\(r\["股票名稱"\]\s*\|\|\s*""\)\.toLowerCase\(\)\.includes\(q\)', r'matchMulti(q, r["股票代號"], r["ETF代號"], r["股票名稱"])', text)

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)

print("Patched successfully")
