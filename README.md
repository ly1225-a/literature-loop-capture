# Literature Loop Capture

这是一个给Codex使用的公开文献检索与全文整理skill。它不是一次性“抓很多论文”，而是把文献综述做成一个可审阅、可批注、可迭代的loop：先做OpenAlex元数据grounding，再由agent写queryplan，用户审阅批准后，再通过OpenCLI控制的已登录浏览器去出版社页面检索、筛选、抓取全文，最后生成阅读笔记、coveragereview、overview和LLMWiki导出。

当前默认支持：

- OpenAlex元数据grounding
- Codex内置浏览器中的HTMLquery-plan审阅与现场批注
- OpenCLI加已登录Chromeprofile的出版社检索与网页全文抓取
- ScienceDirect/Elsevier、ACS、Wiley、Springer的直连出版社网页路径
- Science、Nature、arXiv等PDF后续路径
- MinerUAPI解析用户补充PDF
- `_knowledge/`人类预览层
- `llm_wiki_project_export/`LLMWiki导入包

## 核心流程

```text
研究问题
-> OpenAlex宽检索grounding
-> agent写queryplan
-> HTMLreviewpage在Codex内置浏览器打开
-> 用户现场批注、修正、批准
-> OpenCLI搜索出版社结果页
-> 标题/snippet/abstract筛选
-> 抓取网页全文、图表、表格
-> subquestion阅读笔记
-> 提取high-valueseeds和推荐references
-> coveragereview打分
-> 足够则结束；不足则生成下一轮broaddiscoveryquery
-> 所有subquestion结束后，统一处理references/PDF/manualholds
-> 生成overview、_knowledge和LLMWiki导出
```

## 设计重点

1. **Queryplan先审阅再执行**

   queryplan会生成HTML页面，并在Codex内置浏览器里打开。用户可以现场批注、要求拆分subquestion、修改query，或指出明显误配。修正后再批准，避免检索一开始就跑偏。

2. **每个subquestion是闭环**

   每个subquestion单独经历：

   ```text
   discovery -> abstract preview -> capture -> reading notes
   -> seed/reference/gap extraction -> coverage review
   ```

   只有coverage足够，或者明确`stop_with_gaps`/`blocked`，这个subquestion才算结束。

3. **第二轮以后只让broaddiscovery进入新搜索**

   reference、seed、exacttarget会被保存到ledger，但不会被Python自动当成新broadquery乱搜。下一轮真正进入出版社搜索的，只是经过agent解释和用户审阅的短英文broaddiscoveryquery。

4. **Exacttarget来自研究过程，并且必须复查**

   exacttarget通常来自阅读笔记里的high-valueseed、已抓文章推荐的reference、coveragegap、queryrationale、用户批注，或者某个subquestion里反复出现的数据库、方法、benchmark、ontology、论文标题。它代表“这个东西值得单独确认”，但不等于Python可以直接拿字符串去OpenAlex自动配对。

   agent需要先复查exacttarget的含义，必要时扩写成数据库、资源、论文标题、DOI、作者或venue级别的明确目标。OpenAlex返回候选后，还要核对标题、DOI、venue和上下文。未通过`agent_openalex_verified=true`的exacttarget不进入后续抓取队列。

5. **四个核心出版社默认抓网页全文，不批量抓PDF**

   对ScienceDirect/Elsevier、ACS、Wiley、Springer，默认路径是打开文章网页并抽取正文、章节、图表和表格，而不是一次性批量下载PDF。这样做更适合agent阅读：网页DOM通常保留标题层级、摘要、图表说明、表格和引用信息，便于后续写readingnotes、抽seed、做coveragereview。批量PDF下载也更容易触发下载限制、验证码或临时封锁，所以PDF更适合作为补充路径，而不是主路径。

6. **最后产物可以进入LLMWiki**

   导出包会把正文Markdown、阅读笔记、图表、表格、queryjourney、subquestionoverview、ledger等整理成LLMWikiprojectexport。文章级source页面交给LLMWikiingest生成，项目wiki本身只保留导航、过程骨架和rawsources。导入LLMWiki或写项目说明时，可以把这个公开skill的GitHub链接作为方法来源：[https://github.com/ly1225-a/literature-loop-capture](https://github.com/ly1225-a/literature-loop-capture)。

## 快速开始

先准备本地运行环境，并把公开skill链接到Codex：

```bash
./scripts/setup_loop_runtime.sh
source .venv/bin/activate
ln -sfn "$PWD/literature-loop-capture" "$HOME/.codex/skills/literature-loop-capture"
```

配置OpenCLI，并确认它能控制你已经登录的Chromeprofile：

```bash
opencli doctor
opencli profile list
opencli profile use <your-profile>
```

然后在这个Chrome/OpenCLIprofile里登录后续要用的出版社网站，例如ScienceDirect、ACS、Wiley、Springer、Science、Nature。脚本不会处理账号密码，只复用你已经登录好的浏览器状态。

环境变量只放在本地shell或direnv中，不要提交真实key：

```bash
export OPENALEX_API_KEY="..."
export MINERU_API_KEY="..."
```

之后主要通过和Codexagent对话启动流程，而不是手动拼脚本参数。例如：

```text
Use literature-loop-capture to review "<your topic>",
years 2021-2026, publishers Elsevier, ACS, Wiley, Springer.
Run doctor first, create the OpenAlex-grounded query plan,
open the query-plan review page in the Codex built-in browser,
wait for my approval, then continue the OpenCLI discovery/capture loop.
```

agent会先做OpenAlexgrounding，写出queryplan，然后生成HTMLreviewpage。这个页面应该在Codex内置浏览器里打开，方便你现场批注、指出query或subquestion的问题、要求拆分或修改。不要让reviewpage自动跳到普通Chrome，因为普通Chrome/OpenCLIprofile要保留给出版社登录状态。

你批准queryplan后，agent会继续运行discovery、abstractpreview、capture、readingnotes、coveragereview和后续iteration。你可以随时问：

```text
现在流程到哪一步了？
还有哪些manual hold？
打开当前query plan/coverage review给我看。
继续下一步。
```

## PDF与MinerU

四个核心出版社的主路径是网页全文抽取，不是PDF下载。PDF主要用于三类情况：自动网页抓取拿不到全文、文章只适合PDF路径、用户已经手动补充PDF。

当需要补充PDF时，先生成`_knowledge/`：

```bash
python literature-loop-capture/scripts/knowledge_staging.py "LiteratureCaptures/<run-folder>"
```

用户把PDF放入对应subquestion：

```text
_knowledge/subquestions/<subquestion_id>/manual_pdf_dropbox/
```

之后同步回标准articlefolder，运行MinerUAPInormalize，再让reading-note流程继续。标准流程只使用MinerUAPI，不要求本地MinerU模型。

如果需要额外抓PDF，可以把[Rimagination/scansci-pdf](https://github.com/Rimagination/scansci-pdf)作为外部PDF抓取工具使用。建议把这类PDF抓取当成补充步骤：先用本skill完成出版社网页全文、阅读笔记和coverage判断，再针对manualholds或确实缺全文的DOI补PDF。

## LLMWiki导出

最终overview和subquestionsummaries完成后导出：

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

`raw/sources/articles/**/article.md`是LLMWikiingest的主要输入。PDF不再复制进LLMWiki；抓取或MinerU规范化后的Markdown才是导入源。图像和表格放在`raw/assets/`，并在articleMarkdown中链接或嵌入。

导入LLMWiki后，建议在项目说明或overview里保留公开方法链接：[https://github.com/ly1225-a/literature-loop-capture](https://github.com/ly1225-a/literature-loop-capture)。这样后续agent能知道rawsources、queryjourney、ledger和article.md的组织方式来自哪个skill。

## 边界

- OpenAlex只用于元数据grounding，不是全文来源。
- Python可以校验、去重、路由、导出，但不能替代agent做语义判断。
- queryplan、coverage、referencefollow-up都必须留下可审计文件。
- 出版社登录状态由用户自己的Chrome/OpenCLIprofile提供，脚本不处理密码。
- CAPTCHA、登录失效、PDFviewer下载失败都记录blocker，不绕过。
