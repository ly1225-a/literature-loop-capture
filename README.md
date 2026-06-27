# Literature Loop Capture

这是一个给 agent 使用的公开文献检索与全文整理 skill。它不是一次性批量抓论文，而是把文献综述拆成可审阅、可批注、可迭代的 loop：先用 OpenAlex 做元数据 grounding，再由 agent 写 query plan；用户审阅批准后，再通过 OpenCLI 控制的已登录浏览器进入出版社页面检索、筛选、抓取网页全文，最后生成 reading notes、coverage review、overview 和 LLM Wiki 导出。

当前默认支持：

- OpenAlex 元数据 grounding
- agent 内置浏览器或 localhost 页面中的 HTML query plan 审阅与现场批注
- OpenCLI 加已登录 Chrome profile 的出版社检索与网页全文抓取
- ScienceDirect/Elsevier、ACS、Wiley、Springer 的直连出版社网页路径
- Science、Nature、arXiv 等 PDF 后续路径
- MinerU API 解析用户补充 PDF
- `_knowledge/` 人类预览层
- `llm_wiki_project_export/` LLM Wiki 导入包

## 相关项目

- LLM Wiki 导入与阅读端：[nashsu/llm_wiki](https://github.com/nashsu/llm_wiki)
- OpenCLI 浏览器控制：[jackwener/opencli](https://github.com/jackwener/opencli)
- 可选 PDF 补充抓取：[Rimagination/scansci-pdf](https://github.com/Rimagination/scansci-pdf)

## 核心流程

```text
研究问题
-> OpenAlex 宽检索 grounding
-> agent 写 query plan
-> HTML review page 在 agent 内置浏览器或 localhost 页面打开
-> 用户现场批注、修正、批准
-> OpenCLI 搜索出版社结果页
-> title / snippet / abstract 筛选
-> 抓取网页全文、章节、图表、表格
-> subquestion reading notes
-> 提取 high-value seeds、references、gaps
-> coverage review
-> 足够则结束；不足则生成下一轮 broad discovery query
-> 所有 subquestion 结束后，统一处理 references / PDF / manual holds
-> 生成 overview、_knowledge 和 LLM Wiki 导出
```

## 设计重点

1. **Query plan 先审阅再执行**

   query plan 会生成 HTML 页面，并在 agent 内置浏览器或 localhost 页面里打开。用户可以现场批注、要求拆分 subquestion、修改 query，或指出明显误配。修正后再批准，避免检索一开始就跑偏。

2. **每个 subquestion 是闭环**

   每个 subquestion 单独经历：

   ```text
   discovery -> abstract preview -> capture -> reading notes
   -> seed / reference / gap extraction -> coverage review
   ```

   只有 coverage 足够，或者明确 `stop_with_gaps` / `blocked`，这个 subquestion 才算结束。

3. **第二轮以后只让 broad discovery 进入新搜索**

   reference、seed、exact target 会被保存到 ledger，但不会被 Python 自动当成新 broad query 乱搜。下一轮真正进入出版社搜索的，只是经过 agent 解释和用户审阅的短英文 broad discovery query。

4. **Exact target 来自研究过程，并且必须复查**

   exact target 通常来自 reading notes 里的 high-value seed、已抓文章推荐的 reference、coverage gap、query rationale、用户批注，或者某个 subquestion 里反复出现的数据库、方法、benchmark、ontology、论文标题。它代表“这个东西值得单独确认”，但不等于 Python 可以直接拿字符串去 OpenAlex 自动配对。

   agent 需要先复查 exact target 的含义，必要时扩写成数据库、资源、论文标题、DOI、作者或 venue 级别的明确目标。OpenAlex 返回候选后，还要核对标题、DOI、venue 和上下文。未通过 `agent_openalex_verified=true` 的 exact target 不进入后续抓取队列。

5. **四个核心出版社默认抓网页全文，不批量抓 PDF**

   对 ScienceDirect/Elsevier、ACS、Wiley、Springer，默认路径是打开文章网页并抽取正文、章节、图表和表格，而不是一次性批量下载 PDF。网页 DOM 通常保留标题层级、摘要、图表说明、表格和引用信息，更适合 agent 后续写 reading notes、抽 seed、做 coverage review。批量 PDF 下载也更容易触发下载限制、验证码或临时封锁，所以 PDF 更适合作为补充路径，而不是主路径。

6. **最后产物可以进入 LLM Wiki**

   导出包会把正文 Markdown、阅读笔记、图表、表格、query journey、subquestion overview、ledger 等整理成 LLM Wiki project export。文章级 source pages 交给 LLM Wiki ingest 生成，项目 wiki 本身只保留导航、过程骨架和 raw sources。导入端可参考 [nashsu/llm_wiki](https://github.com/nashsu/llm_wiki)。

## 快速开始

先准备本地运行环境，并把公开 skill 链接到你的 agent 技能目录。下面以 `~/.codex/skills` 为例；其他 agent 环境可以改成自己的 skill 目录。

```bash
./scripts/setup_loop_runtime.sh
source .venv/bin/activate
ln -sfn "$PWD/literature-loop-capture" "$HOME/.codex/skills/literature-loop-capture"
```

Claude Code 用户可以参考 `.claude/commands/literature-loop-capture.md`，让 Claude 读取 `literature-loop-capture/SKILL.md` 并按同一套 loop 执行。

配置 OpenCLI，并确认它能控制你已经登录的 Chrome profile：

```bash
opencli doctor
opencli profile list
opencli profile use <your-profile>
```

然后在这个 Chrome / OpenCLI profile 里登录后续要用的出版社网站，例如 ScienceDirect、ACS、Wiley、Springer、Science、Nature。脚本不会处理账号密码，只复用你已经登录好的浏览器状态。

环境变量只放在本地 shell 或 direnv 中，不要提交真实 key：

```bash
export OPENALEX_API_KEY="..."
export MINERU_API_KEY="..."
```

之后主要通过和 agent 对话启动流程，而不是手动拼脚本参数。例如：

```text
Use literature-loop-capture to review "<your topic>",
years 2021-2026, publishers Elsevier, ACS, Wiley, Springer.
Run doctor first, create the OpenAlex-grounded query plan,
open the query-plan review page in the agent browser or localhost review page,
wait for my approval, then continue the OpenCLI discovery/capture loop.
```

agent 会先做 OpenAlex grounding，写出 query plan，然后生成 HTML review page。这个页面应该在 agent 内置浏览器或 localhost 页面里打开，方便你现场批注、指出 query 或 subquestion 的问题、要求拆分或修改。不要让 review page 自动跳到普通 Chrome，因为普通 Chrome / OpenCLI profile 要保留给出版社登录状态。

你批准 query plan 后，agent 会继续运行 discovery、abstract preview、capture、reading notes、coverage review 和后续 iteration。你可以随时问：

```text
现在流程到哪一步了？
还有哪些 manual hold？
打开当前 query plan / coverage review 给我看。
继续下一步。
```

## PDF 与 MinerU

四个核心出版社的主路径是网页全文抽取，不是 PDF 下载。PDF 主要用于三类情况：自动网页抓取拿不到全文、文章只适合 PDF 路径、用户已经手动补充 PDF。

当需要补充 PDF 时，先生成 `_knowledge/`：

```bash
python literature-loop-capture/scripts/knowledge_staging.py "LiteratureCaptures/<run-folder>"
```

用户把 PDF 放入对应 subquestion：

```text
_knowledge/subquestions/<subquestion_id>/manual_pdf_dropbox/
```

之后同步回标准 article folder，运行 MinerU API normalize，再让 reading-note 流程继续。标准流程只使用 MinerU API，不要求本地 MinerU 模型。

本 skill 不内置 `scansci-pdf`，也不把批量 PDF 抓取作为主流程。如果需要额外获取 PDF，可以把 [Rimagination/scansci-pdf](https://github.com/Rimagination/scansci-pdf) 作为可选外部途径；获取到的 PDF 再放入 `_knowledge/subquestions/<subquestion_id>/manual_pdf_dropbox/`，之后用本 skill 的 MinerU API normalize 流程接回 article folder。

## LLM Wiki 导出

最终 overview 和 subquestion summaries 完成后导出：

```bash
python literature-loop-capture/scripts/llm_wiki_export.py \
  "LiteratureCaptures/<run-folder>" \
  --output-root "<your-export-root>"
```

导出结构大致是：

```text
<project-name>/
  README.md
  purpose.md
  schema.md
  manifest.csv
  manifest.json
  wiki/
    index.md
    overview.md
    log.md
    subquestions/
    queries/
    ledgers/
  raw/
    sources/
      dossier/
      articles/
    assets/
      articles/
    provenance/
```

`raw/sources/articles/**/article.md` 是 LLM Wiki ingest 的主要输入。PDF 不再复制进 LLM Wiki；抓取或 MinerU 规范化后的 Markdown 才是导入源。图像和表格放在 `raw/assets/`，并在 article Markdown 中链接或嵌入。

## 边界

- OpenAlex 只用于元数据 grounding，不是全文来源。
- Python 可以校验、去重、路由、导出，但不能替代 agent 做语义判断。
- query plan、coverage、reference follow-up 都必须留下可审计文件。
- 出版社登录状态由用户自己的 Chrome / OpenCLI profile 提供，脚本不处理密码。
- CAPTCHA、登录失效、PDF viewer 下载失败都记录 blocker，不绕过。
