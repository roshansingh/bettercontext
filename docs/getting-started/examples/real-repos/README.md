# Real Repositories for Examples

**Last updated**: 2026-05-25

This directory contains setup scripts for well-known open-source repositories used in SuperContext examples. These projects were chosen because they are real, actively maintained, structurally diverse, and representative of common use cases.

All setup scripts clone the repository to your local machine, prepare it for analysis, and guide you through building a knowledge graph snapshot.

---

## Which Repositories and Why?

SuperContext examples use three projects:

| Repository | Language | Why Included | Size | Time to Build |
|------------|----------|-------------|------|----------------|
| **Flask** | Python | Web framework; well-structured; diverse module patterns | ~2,000 functions | 3-5 min |
| **React** | TypeScript/JSX | Modern UI library; component architecture; extensive type annotations | ~1,500 components | 4-6 min |
| **Microservice Pair** | Python | Two simple services demonstrating cross-repo linking and deployment dependencies | Fixture (~500 lines) | 2-3 min |

These three projects cover:
- **Single-repo Python analysis** (Flask)
- **TypeScript/JavaScript analysis** (React)
- **Multi-repo and cross-service analysis** (Microservice Pair)

---

## Flask (Python Web Framework)

**What it is**: A popular, lightweight Python web framework for building HTTP APIs and web applications.

**Why included**: 
- Real production codebase maintained by an active community
- Well-organized module structure with clear dependency chains
- ~2,000 functions providing rich call graphs
- Pure Python (no complex build steps)

### Setup

```bash
bash examples/real-repos/setup-flask.sh
```

**What happens**:
1. Clones Flask to `temp/flask` (or your choice of directory)
2. Verifies Python 3.11+ is available
3. Runs SuperContext's KG build initialization
4. Prints the repository path for use in example scripts

**Output**:
```
✓ Flask cloned to: /Users/you/bettercontext/temp/flask
✓ Ready for analysis with SuperContext
Next step: bash examples/01-build/build-kg-single-repo.sh
```

**Time**: ~2 minutes (mostly git cloning)

### Using Flask in Examples

After setup, point example scripts at the Flask clone:

```bash
bash examples/01-build/build-kg-single-repo.sh
# When prompted: /Users/you/bettercontext/temp/flask

bash examples/02-query/query-common-patterns.sh
# Point at the snapshot created above
```

### Typical Queries

With Flask analyzed, you can run:

- **Find callers of a request handler**: 
  ```bash
  python -m source.scripts.query_kg --snapshot data/kg_runs/flask \
    find-callers flask.blueprints.Blueprint.register --limit 10
  ```

- **Trace dependency chains**:
  ```bash
  python -m source.scripts.query_kg --snapshot data/kg_runs/flask \
    find-callees flask.app.Flask.__init__
  ```

- **Blast radius analysis**: Understand what breaks if you modify a core function

---

## React (TypeScript UI Library)

**What it is**: The popular JavaScript/TypeScript library for building user interfaces with components and hooks.

**Why included**:
- Real production codebase maintained by Meta and the React community
- Modern TypeScript with extensive type annotations
- Component-driven architecture with clear hierarchies
- Demonstrates multi-language analysis (JavaScript + TypeScript)

### Setup

```bash
bash examples/real-repos/setup-react.sh
```

**What happens**:
1. Clones React to `temp/react` (or your choice of directory)
2. Verifies Node.js 16+ is available (for TypeScript analysis)
3. Installs TypeScript compiler if needed
4. Runs SuperContext's KG build initialization
5. Prints the repository path for use in example scripts

**Output**:
```
✓ React cloned to: /Users/you/bettercontext/temp/react
✓ Node.js v18.0.0 detected
✓ Ready for analysis with SuperContext
Next step: bash examples/01-build/build-kg-single-repo.sh
```

**Time**: ~3-4 minutes (including npm install for TypeScript tools)

**Prerequisites**: Node.js 16+ is required for TypeScript analysis. If you skip it, you can still analyze JavaScript files only.

### Using React in Examples

After setup, point example scripts at the React clone:

```bash
bash examples/01-build/build-kg-single-repo.sh
# When prompted: /Users/you/bettercontext/temp/react

bash examples/02-query/query-common-patterns.sh
# Point at the snapshot created above
```

### Typical Queries

With React analyzed, you can run:

- **Find uses of a React hook**:
  ```bash
  python -m source.scripts.query_kg --snapshot data/kg_runs/react \
    find-callers react.hooks.useState --limit 10
  ```

- **Understand component dependencies**: Which components depend on a shared utility

- **Trace type imports**: How TypeScript definitions flow through the codebase

---

## Microservice Pair (Fixture)

**What it is**: Two simple, self-contained microservices (user-service and auth-service) created as a learning fixture.

**Why included**:
- Demonstrates multi-repository analysis (fleet view)
- Shows cross-repo linking and service boundaries
- Includes deployment and configuration examples
- Small enough to understand completely in one sitting (~500 lines per service)
- Perfect for learning cross-service change-safety analysis

### Setup

```bash
bash examples/real-repos/setup-microservice-example.sh
```

**What happens**:
1. Creates fixture directory `temp/microservices/`
2. Generates two service repositories:
   - `user-service`: User management and profile API
   - `auth-service`: Authentication and token management
3. Adds deployment manifests and service definitions
4. Prepares both for multi-repo KG analysis

**Output**:
```
✓ Microservice example created at: /Users/you/bettercontext/temp/microservices
├── user-service/
├── auth-service/
✓ Ready for multi-repo analysis with SuperContext
Next step: bash examples/01-build/build-kg-multi-repo.sh
```

**Time**: ~1-2 minutes (no cloning, just fixture generation)

### Using Microservices in Examples

After setup, use the multi-repo build script:

```bash
bash examples/01-build/build-kg-multi-repo.sh
# When prompted, point at:
#   /Users/you/bettercontext/temp/microservices/user-service
#   /Users/you/bettercontext/temp/microservices/auth-service

bash examples/02-query/query-dependency-chains.sh
# Point at the multi-repo snapshot created above
```

### Typical Queries

With both services analyzed together, you can run:

- **Cross-repo find-callers**: Which services call `user-service.handlers.get_user`?
  ```bash
  python -m source.scripts.query_kg --snapshot data/kg_runs/microservices \
    find-callers user_service.handlers.get_user
  ```

- **Service dependency chains**: Trace what auth-service depends on from user-service

- **Deployment impact analysis**: What breaks when you deploy a change to auth-service?

---

## Using Examples in Workflows

Examples are organized to follow the learning paths in the main [README.md](../README.md).

### Learning Path 1: Using SuperContext (30-45 minutes)

1. **Setup a repository**:
   ```bash
   bash examples/real-repos/setup-flask.sh
   ```

2. **Follow [Setup and First KG](../../03-workflows/setup-and-first-kg.md)**, using the Flask path

3. **Build a knowledge graph** with the Flask clone:
   ```bash
   bash examples/01-build/build-kg-single-repo.sh
   ```

4. **Follow [Query Your Repo](../../03-workflows/query-your-repo.md)**, using the KG snapshot you built

### Learning Path 2: Multi-Repository Analysis

1. **Setup microservices**:
   ```bash
   bash examples/real-repos/setup-microservice-example.sh
   ```

2. **Build a multi-repo snapshot**:
   ```bash
   bash examples/01-build/build-kg-multi-repo.sh
   ```

3. **Run cross-repo queries** to understand dependencies

### Learning Path 3: TypeScript/JavaScript Analysis

1. **Setup React**:
   ```bash
   bash examples/real-repos/setup-react.sh
   ```

2. **Build a TypeScript KG**:
   ```bash
   bash examples/01-build/build-kg-typescript.sh
   ```

3. **Run TypeScript-specific queries** (component dependencies, type imports)

---

## Alternatives: Use Your Own Repository

You don't have to use these examples! Every script in examples/ can work with your own code:

### Option A: Use Your Own Repository

Instead of running a setup script, just point the build scripts at your codebase:

```bash
bash examples/01-build/build-kg-single-repo.sh
# When prompted: /path/to/your/repo
```

The scripts will adapt automatically (no setup script needed).

### Option B: Adjust Snapshot Paths

All example scripts look for snapshots in `data/kg_runs/`. If you build to a different location, pass the `--snapshot` flag:

```bash
python -m source.scripts.query_kg --snapshot /custom/path/snapshot \
  find-callers module.function
```

### Option C: Mix and Match

Use Flask for learning the basics, then switch to your own code:

```bash
bash examples/real-repos/setup-flask.sh     # Learn with Flask
bash examples/01-build/build-kg-single-repo.sh

# Later, switch to your own code
bash examples/01-build/build-kg-single-repo.sh
# Point at: /path/to/your/repo
```

---

## Troubleshooting Setup Scripts

### Script fails to clone (Flask or React)

**Symptom**: "git clone failed" or "connection timeout"

**Solutions**:
- Check internet connection: `ping github.com`
- Clone manually: `git clone https://github.com/pallets/flask.git temp/flask`
- Check git is installed: `git --version`

### Python/Node.js version errors

**Symptom**: "Python 3.11 not found" or "Node.js 16+ not found"

**Solutions**:
- Install Python 3.11+: See [Setup and First KG](../../03-workflows/setup-and-first-kg.md)
- Install Node.js: `brew install node` or [nodejs.org](https://nodejs.org/)
- Verify: `python3 --version` and `node --version`

### Disk space errors

**Symptom**: "No space left on device" during clone

**Solutions**:
- Flask: ~200 MB
- React: ~300 MB
- Microservices: ~5 MB
- Ensure you have ~1 GB free before starting

### Script can't find temporary directory

**Symptom**: "Directory temp/ doesn't exist"

**Solutions**:
- Setup scripts create `temp/` automatically
- If missing, create it: `mkdir -p temp`
- Setup scripts should be run from the SuperContext repo root

---

## Next Steps

After setting up a repository:

1. **Build a knowledge graph**: See [01-build/](../01-build/) examples
2. **Query the KG**: See [02-query/](../02-query/) examples
3. **Evaluate coverage**: See [03-coverage/](../03-coverage/) examples
4. **Follow workflow guides**: [Setup and First KG](../../03-workflows/setup-and-first-kg.md) and beyond

---

## Questions?

- **Setup script questions**: Check the [Troubleshooting](#troubleshooting-setup-scripts) section
- **Example usage questions**: See [../README.md](../README.md#how-to-run-examples)
- **Workflow questions**: See [../../03-workflows/](../../03-workflows/)
- **General SuperContext questions**: See [../../GLOSSARY.md](../../GLOSSARY.md)
