# Dependencies and data attribution

- [smolagents](https://github.com/huggingface/smolagents): Apache-2.0; provides tools, tool schemas and the model interface. The application implements its bounded Agent Loop.
- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code): design reference for model/tool iteration and context compaction. The operational tools, state machine and execution policies are implemented in this repository.
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): MIT; provides stdio protocol client/server and schema support. This release targets the tested 1.x API.
- [RCAEval](https://github.com/phamquiluan/RCAEval) / [dataset](https://huggingface.co/datasets/phamquiluan/RCAEval): upstream declares its own code and datasets MIT; this project imports public data, not the benchmark's separately licensed third-party algorithms.
- httpx, Pydantic, Typer, Python-dotenv, PyArrow, pandas and Hugging Face Hub retain their own licenses. Exact installed dependency versions are captured in requirements.lock.txt.

RCAEval citation:

Luan Pham et al., *RCAEval: A Benchmark for Root Cause Analysis of Microservice Systems with Telemetry Data*, WWW 2025 Companion. https://doi.org/10.1145/3701716.3715290

Dataset revision, seed and file hashes are recorded by the importer. Raw data and labels are downloaded locally and excluded from Git. Follow the upstream dataset terms and citation when distributing derived data.
