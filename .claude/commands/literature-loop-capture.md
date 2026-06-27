# Literature Loop Capture

Use this command when the user asks Claude to run a literature review loop from
this repository.

Read `literature-loop-capture/SKILL.md` first, then follow its workflow:

1. Run `scripts/setup_loop_runtime.sh` if the local `.venv` is missing.
2. Confirm `opencli doctor` works and the user has selected a Chromeprofile
   logged into the needed publisher sites.
3. Create an OpenAlex-grounded queryplan.
4. Serve `query-plan-review.html` on localhost and ask the user to review it.
5. After approval, run OpenCLI publisher discovery/capture, readingnotes,
   coveragereview, iteration, PDF/MinerU fallback, and LLMWiki export as needed.

Do not use the normal Chrome/OpenCLIprofile for queryplan review pages; keep it
available for publisher authentication.
