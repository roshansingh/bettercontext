# Getting Started Examples

**Last updated**: 2026-05-25

This directory contains runnable examples for each SuperContext workflow and feature. Every example is self-contained, documented, and designed to work on real codebases.

Use these examples alongside the workflow guides in [03-workflows/](../03-workflows/) to see SuperContext in action.

---

## Prerequisites

Before running any example, ensure you have the following installed:

### Required

- **Python 3.11+**: SuperContext's ingestion engine
  ```bash
  python3 --version
  ```

- **git**: For cloning repositories and resolving commit hashes
  ```bash
  git --version
  ```

### Recommended

- **Node.js 16+**: For analyzing TypeScript and JavaScript repositories
  ```bash
  node --version
  ```

If you haven't completed the [Setup and First KG](../03-workflows/setup-and-first-kg.md) guide, you'll also need to:
1. Clone the SuperContext repository
2. Install dependencies via `pip install -e .`
3. Verify your Python environment works

All examples assume you're running from the SuperContext repository root directory (`bettercontext/`).

---

## Examples Organization

This directory contains five numbered example sections, plus a `real-repos` subdirectory with setup scripts for well-known open-source projects.

### 01-build

**Building Knowledge Graphs from Real Repositories**

Examples for creating knowledge graph snapshots from Python and TypeScript codebases.

- **build-kg-single-repo.sh**: Clone a repository and build a complete KG snapshot
- **build-kg-multi-repo.sh**: Build a KG across multiple repositories (fleet view)
- **build-kg-typescript.sh**: Build a KG from a TypeScript/JavaScript repository

**When to use**: Start here when you want to analyze your own codebase for the first time.

### 02-query

**Querying Snapshots and Analyzing Results**

Examples for running common queries against a knowledge graph and interpreting results.

- **query-common-patterns.sh**: Find callers, callees, blast-radius queries
- **query-dependency-chains.sh**: Trace module dependency chains
- **query-top-dependencies.sh**: Identify your repo's heaviest dependencies

**When to use**: After building a KG, use these to explore and understand the dependency graph.

### 03-coverage

**Running the Coverage Pipeline**

Examples for evaluating knowledge graph completeness and identifying gaps.

- **coverage-full-pipeline.sh**: Build snapshot, compute metrics, generate report
- **coverage-metrics-only.sh**: Compute coverage metrics for an existing snapshot
- **coverage-report-rendering.sh**: Generate an HTML coverage report

**When to use**: Understand what your KG covers, validate instrumentation, and find missing extractors.

### 04-extend

**Writing Extractors and Customizing SuperContext**

Examples and templates for extending SuperContext to new languages or frameworks.

- **python-extractor-template.py**: Skeleton for a custom Python AST extractor
- **typescript-extractor-template.ts**: Skeleton for a custom TypeScript extractor
- **write-custom-extractor.sh**: Step-by-step walkthrough of the extractor API

**When to use**: Extend SuperContext to analyze languages or patterns not yet supported.

### 05-mcp

**MCP Server and Tool Integration**

Examples for running the MCP server and building tools on top of SuperContext.

- **start-mcp-server.sh**: Launch the local MCP server for development
- **query-via-mcp.sh**: Run queries through the MCP protocol
- **mcp-client-example.py**: Build a custom MCP client

**When to use**: Integrate SuperContext into your IDE, agent, or custom tooling.

### real-repos

**Setup Scripts for Real Open-Source Projects**

Reproducible setup scripts for well-known codebases, used in examples above.

- **setup-flask.sh**: Clone and prepare Flask (Python web framework)
- **setup-react.sh**: Clone and prepare React (TypeScript UI library)
- **setup-microservice-example.sh**: Create example microservices for cross-repo analysis

**When to use**: Run these first if you want to follow along with examples before analyzing your own code.

See [real-repos/README.md](./real-repos/README.md) for details on each project and why it's included.

---

## How to Run Examples

Every example is a **bash script** that includes:
- Clear comments explaining what it does
- Prerequisite checks (Python version, git availability)
- Step-by-step operations with output
- Where to find the results

### Basic Usage

```bash
bash examples/01-build/build-kg-single-repo.sh
```

Each script:
1. **Validates prerequisites** — Stops with a clear error if Python, git, or Node.js are missing
2. **Downloads or clones as needed** — Most examples clone a repo to a temporary location
3. **Produces output** — Results go to `data/kg_runs/` or `docs/evaluation/runs/`
4. **Is idempotent** — Running the same script twice is safe (overwrites results)

### Example: Build Your First Knowledge Graph

```bash
bash examples/01-build/build-kg-single-repo.sh
```

This script will:
- Prompt you to choose a repository (or use Flask as default)
- Clone it to a temporary directory
- Build a complete KG snapshot
- Print the snapshot directory path
- Show you the next step: running queries

**Time**: ~3-5 minutes (varies by repo size)

### Example: Query a Knowledge Graph

After building a snapshot:

```bash
bash examples/02-query/query-common-patterns.sh
```

This script will:
- Ask you to specify the snapshot directory
- Run find-callers, find-callees, and blast-radius queries
- Display results in an easy-to-read format
- Explain what each query means

**Time**: ~1 minute

### Example: Evaluate Coverage

```bash
bash examples/03-coverage/coverage-full-pipeline.sh
```

This script will:
- Ensure a snapshot exists (or build one)
- Compute coverage metrics
- Generate an HTML report
- Open the report in your browser

**Time**: ~2 minutes

---

## Quick Start Path

New to SuperContext? Follow this three-command sequence:

1. **Setup a real repository**:
   ```bash
   bash examples/real-repos/setup-flask.sh
   ```
   This clones Flask and prepares it. Time: ~2 minutes.

2. **Build a knowledge graph**:
   ```bash
   bash examples/01-build/build-kg-single-repo.sh
   ```
   Point it at the Flask clone. Time: ~3-5 minutes.

3. **Run queries**:
   ```bash
   bash examples/02-query/query-common-patterns.sh
   ```
   Point it at the snapshot you just built. Time: ~1 minute.

**Total time**: 10 minutes. You'll have a working knowledge graph and understand the core workflow.

---

## Troubleshooting

### Script fails to download or clone a repository

**Symptom**: Error like "git clone failed" or "connection timeout"

**Causes**:
- No internet connection
- Repository URL is incorrect
- GitHub API rate limit (for large repos)
- Insufficient disk space

**Solutions**:
- Check your internet connection
- Verify git is installed and working (`git --version`)
- Clone the repository manually: `git clone <url>`
- Ensure you have ~500 MB free disk space

### Python import error (e.g., "No module named 'source'")

**Symptom**: Error like `ModuleNotFoundError: No module named 'source'`

**Causes**:
- SuperContext not installed
- Wrong Python version (need 3.11+)
- Running from the wrong directory

**Solutions**:
- Follow [Setup and First KG](../03-workflows/setup-and-first-kg.md) to install SuperContext
- Verify Python version: `python3 --version`
- Run from the SuperContext repo root: `cd bettercontext && bash examples/...`
- Check your virtual environment: `source venv/bin/activate`

### Can't find snapshot directory

**Symptom**: Error like "Snapshot directory not found" or "No such file or directory"

**Causes**:
- Snapshot hasn't been built yet
- Snapshot path is incorrect
- Built to a different location than expected

**Solutions**:
- Run a build example first: `bash examples/01-build/build-kg-single-repo.sh`
- Check where snapshots are stored: `ls -la data/kg_runs/`
- Snapshot paths follow the pattern: `data/kg_runs/<repo-name>` or `data/kg_runs/<run-id>`

### Node.js or TypeScript errors

**Symptom**: Error like "command not found: node" or "TypeScript compiler not found"

**Causes**:
- Node.js not installed
- TypeScript not in PATH
- Wrong Node.js version (need 16+)

**Solutions**:
- Install Node.js: [nodejs.org](https://nodejs.org/) or `brew install node`
- Verify installation: `node --version`
- For TypeScript repos, Node.js is strongly recommended but optional for Python repos

---

## Contributing Examples

Want to add a new example? Follow these guidelines:

### Requirements

- **Self-contained**: Example works without external setup (except running one setup script)
- **Well-commented**: Every step explains what it does and why
- **Tested**: Verify the script works end-to-end before submitting
- **Documented**: Add a brief description in the appropriate section above
- **Error handling**: Include prerequisite checks and clear error messages

### Adding a New Example

1. **Choose the right section**: Is it about building (01), querying (02), coverage (03), extending (04), or MCP (05)?

2. **Create your script**:
   ```bash
   touch examples/XX-section/my-example.sh
   chmod +x examples/XX-section/my-example.sh
   ```

3. **Follow the template**:
   ```bash
   #!/bin/bash
   # Title: What this example does
   # Time: estimated duration
   # Prerequisites: what's needed
   
   set -e  # Exit on error
   
   # Check prerequisites
   if ! command -v python3 &> /dev/null; then
       echo "Error: Python 3 not found. Install it first."
       exit 1
   fi
   
   # Main script body
   echo "Starting example..."
   
   # Example complete
   echo "Done! Next steps: ..."
   ```

4. **Update this README**: Add your example to the appropriate section with a one-line description

5. **Test it**: Run `bash examples/XX-section/my-example.sh` and verify it works

---

## Next Steps

After running examples:

- **Read [Setup and First KG](../03-workflows/setup-and-first-kg.md)** for the complete setup walkthrough
- **Review [Query Your Repo](../03-workflows/query-your-repo.md)** to understand all query types
- **Check [Evaluate Coverage](../03-workflows/evaluate-coverage.md)** to assess KG completeness
- **See [BACKLOG.md](../../../BACKLOG.md)** for upcoming features and known limitations

---

## Questions?

If an example doesn't work or you need help:

1. Check the [Troubleshooting](#troubleshooting) section above
2. Review the script comments: `head -20 examples/XX-section/my-example.sh`
3. Read the relevant workflow guide in [03-workflows/](../03-workflows/)
4. Check [GLOSSARY.md](../GLOSSARY.md) for term definitions
5. Open an issue on GitHub with the error message and script name
