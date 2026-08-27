# Role-targeted CV build.
#
#   make ROLE=ios              build the iOS-targeted CV
#   make ROLE=flutter open     build it and open the PDF
#   make all-roles             build every role at once
#   make explain ROLE=ios      show why each bullet was kept or dropped
#   make audit                 unevidenced claims + verify every repo@sha
#   make fit ROLE=ios          auto-trim bullets until it fits one page
#   make roles                 list available roles
#   make watch ROLE=ios        rebuild on every save
#   make clean
#
# Roles live in roles/*.toml. Content lives in content/*.toml.
# sections/ is generated -- never edit it by hand.

ROLE ?= ios

# MacTeX installs to /Library/TeX/texbin, which a non-login shell may not
# have on PATH. Two separate fixes are needed:
#   1. export PATH  -- latexmk spawns pdflatex via sh, so the engine must
#                      be findable by child processes.
#   2. absolute path -- make direct-execs simple recipes using its OWN
#                      PATH, ignoring the exported one.
TEXBIN      := /Library/TeX/texbin
export PATH := $(if $(wildcard $(TEXBIN)),$(TEXBIN):$(PATH),$(PATH))
LATEXMK_BIN := $(if $(wildcard $(TEXBIN)/latexmk),$(TEXBIN)/latexmk,latexmk)

MAIN     := main.tex
OUTDIR   := build
PDF      := $(OUTDIR)/main.pdf
OUTNAME  := Anas_Nasr_Mostafa_CV_$(ROLE).pdf
DIST     := $(OUTDIR)/$(OUTNAME)
CONTENT  := $(wildcard content/*.toml) $(wildcard roles/*.toml) scripts/render.py
LATEXMK  := $(LATEXMK_BIN) -pdf -interaction=nonstopmode -file-line-error -synctex=1 -outdir=$(OUTDIR)

.PHONY: all render open watch explain audit fit roles all-roles check clean

all: $(DIST)

# Render is not a file target: the role can change without any file
# changing, so the sections must be regenerated on every invocation.
render:
	@python3 scripts/render.py --role $(ROLE)

$(DIST): render $(MAIN) styles/resume.sty
	@$(LATEXMK) $(MAIN) >/dev/null
	@cp $(PDF) $(DIST)
	@echo "built $(DIST) ($$(python3 -c "import re,pathlib;\
	  raw=pathlib.Path('$(OUTDIR)/main.log').read_text(errors='replace');\
	  m=re.search(r'Output written on .*?\((\d+) pages?', re.sub(r'\n','',raw));\
	  print(m.group(1) if m else '?')") page)"

open: all
	@open $(DIST)

# latexmk -pvc only watches files LaTeX reads, so it would miss every
# edit to content/*.toml. watch.py polls the real inputs instead.
watch:
	@python3 -u scripts/watch.py --role $(ROLE)

explain:
	@python3 scripts/render.py --role $(ROLE) --explain

roles:
	@echo "available roles:"
	@for f in roles/*.toml; do \
	  n=$$(basename $$f .toml); \
	  t=$$(grep -m1 '^name' $$f | cut -d'"' -f2); \
	  printf "  %-10s %s\n" "$$n" "$$t"; \
	done

all-roles:
	@for f in roles/*.toml; do \
	  $(MAKE) --no-print-directory ROLE=$$(basename $$f .toml) all; \
	done

# Every claim with no evidence reference behind it.
audit:
	@python3 scripts/audit.py --verify

# Rebuild with progressively fewer bullets until the PDF is one page.
fit:
	@python3 scripts/fit.py --role $(ROLE)

check: all
	@python3 -c "import re,pathlib;\
	raw=pathlib.Path('$(OUTDIR)/main.log').read_text(errors='replace');\
	m=re.search(r'Output written on .*?\((\d+) pages?, (\d+) bytes\)', re.sub(r'\n','',raw));\
	p=int(m.group(1)); o=len(re.findall(r'Overfull .hbox', raw));\
	print(f'role=$(ROLE)  pages={p}  overfull={o}');\
	print('OK - fits one page' if p==1 else f'TOO LONG - {p} pages, run: make fit ROLE=$(ROLE)');\
	raise SystemExit(0 if p==1 else 1)"

clean:
	@$(LATEXMK) -C $(MAIN) >/dev/null 2>&1 || true
	@rm -rf $(OUTDIR) sections
	@echo "cleaned build/ and generated sections/"
