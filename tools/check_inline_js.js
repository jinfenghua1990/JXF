// 静态页内联 <script> JS 语法门禁：任一块解析失败即非零退出，阻止镜像构建。
const fs = require("fs");
const path = require("path");
const dir = process.argv[2];
if (!dir) { console.error("用法: node check_inline_js.js <static目录>"); process.exit(2); }
const htmls = fs.readdirSync(dir).filter(function (f) { return f.endsWith(".html"); });
let total = 0, fails = 0;
for (const f of htmls) {
  const html = fs.readFileSync(path.join(dir, f), "utf8");
  const re = /<script([^>]*)>([\s\S]*?)<\/script>/gi;
  let m, block = 0;
  while ((m = re.exec(html)) !== null) {
    if (/\bsrc\s*=/i.test(m[1])) continue; // 外链脚本跳过（本地无外链依赖）
    block++; total++;
    try { new Function(m[2]); console.log("[OK]   " + f + " script#" + block); }
    catch (e) { fails++; console.error("[FAIL] " + f + " script#" + block + ": " + e.message); }
  }
}
if (total === 0) { console.error("未发现内联 script，请人工确认是否改了入口文件名"); process.exit(1); }
if (fails > 0) { console.error("共 " + fails + " 处 JS 语法错误 —— 部署中止"); process.exit(1); }
console.log("JS 语法检查通过（" + total + " 个内联块）");
