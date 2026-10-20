# A 股持仓行情 · 静态网页版

把本地 `LDLChinaStock-V1.1/dashboard` 的主页发布到 GitHub Pages，由 GitHub
Actions 在 A 股交易时段定时抓取行情与衍生指标，生成加密快照。部署后不依赖任何
本地机器是否开机，手机、平板、电脑都可以通过外网访问。

- 站点：<https://greatday4yole.github.io/a-stock-dashboard/>
- 访问密码：由部署者单独保存，不写入本仓库
- 数据：定时刷新，只读；网页端不支持修改组合、回填盘中数据或作废模拟盘记录

## 工作方式

1. GitHub Actions 每 5 分钟运行一次（中国时间 09:00–15:59，工作日），每小时
   做一次完整刷新，收盘后补一次收盘快照。
2. Runner 用仓库 Secret `SOURCE_DATA_KEY` 解密 `bundle/source-data.tar.gz.enc`，
   得到 `config/`、`data/` 和个人指数快照。
3. 启动原版 `scripts/dashboard_server.py`，通过本机回环接口导出所有只读 API。
4. 用仓库 Secret `SITE_PASSWORD` 派生 AES-256-GCM 密钥，把每个 API 快照加密成
   `static-api/*.json.enc`，再发布到 GitHub Pages。
5. 浏览器打开页面后输入访问密码，`static-api-shim.js` 在本地解密数据并交给原版
   `app.js` 渲染。密码不会上传，持仓数据在 GitHub 上始终是密文。

## 安全说明

- 仓库是公开的（GitHub Free 的 Pages 只支持公开仓库），但 `config/`、`data/`
  以加密压缩包形式存在；`site/` 中的 API 数据也是 AES-256-GCM 密文。
- 访问密码和源数据密钥分别存放在 GitHub Actions Secrets 中，不进入代码或
  构建产物。
- 页面通过 HTTPS 提供，设置了 `noindex,nofollow`；请勿在公开场合分享密码。
- 静态部署是只读的。新建/编辑/删除组合、盘中回填、模拟盘作废等写操作会返回
  只读提示。

## 已知限制

- 行情与新闻的时效取决于 GitHub Actions 的调度；交易时段通常每 5 分钟更新，
  非交易时段基本保持收盘快照。
- 云端 Runner 没有同花顺 CLI 的系统钥匙串，因此同花顺官方报价交叉校验会显示
  不可用；页面仍使用腾讯/新浪公开行情，并在顶部保留数据质量提示。
- 盘中历史曲线只保留最近 10 个交易日（可通过 `--history-days` 调整后重建）。
- 持仓数量、现金、负债来自加密包中的账户快照；发生实际交易后需要重新生成加密包
  并推送，云端不会自动读取券商账户。

## 更新代码或持仓数据

在本地项目目录执行：

```bash
cd AStockDashboard-Web
export SOURCE_DATA_KEY='<仓库 Secret 中的源数据密钥>'
./build/sync_from_project.sh ../LDLChinaStock-V1.1
git add -A
git commit -m "refresh dashboard snapshot"
git push
```

`sync_from_project.sh` 会重新复制 `scripts/` 和静态前端文件，并用最新
`config/`、`data/` 重建 `bundle/source-data.tar.gz.enc`。推送后 Actions 会自动
重新发布。

修改访问密码：更新 GitHub Secret `SITE_PASSWORD`，并重新运行一次完整构建即可；
旧密码派生出的浏览器缓存会失效，需要重新输入。
