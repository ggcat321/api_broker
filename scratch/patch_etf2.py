import re

path = '/Users/jeffrey/Downloads/ETF_analyzer.htm'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

text = re.sub(r'String\(r\["股票代號"\]\)\.toLowerCase\(\)\.includes\(q\) \|\|\s*String\(r\["股票名稱"\]\s*\|\|\s*""\)\.toLowerCase\(\)\.includes\(q\)', r'matchMulti(q, r["股票代號"], r["股票名稱"])', text, flags=re.MULTILINE)
text = re.sub(r'String\(r\["股票代號"\]\)\.toLowerCase\(\)\.includes\(q\) \|\|\s*String\(r\["ETF代號"\]\)\.toLowerCase\(\)\.includes\(q\) \|\|\s*String\(r\["股票名稱"\]\s*\|\|\s*""\)\.toLowerCase\(\)\.includes\(q\)', r'matchMulti(q, r["股票代號"], r["ETF代號"], r["股票名稱"])', text, flags=re.MULTILINE)
text = re.sub(r'r\.code\.toLowerCase\(\)\.includes\(q\) \|\|\s*String\(r\.en\s*\|\|\s*""\)\.toLowerCase\(\)\.includes\(q\)', r'matchMulti(q, r.code, r.en)', text, flags=re.MULTILINE)

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)

print("Patched successfully")
