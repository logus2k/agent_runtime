// agent_runtime CI — Phase 01 harness (block_management.md §13).
// Runs the pytest suite (composer/runtime). Extended per phase (graph executor,
// AgentRecord decomposition, skills, loop-type, resources API).
pipeline {
  agent any
  options { timestamps() }
  stages {
    stage('pytest') {
      steps {
        sh 'python3 -m pytest -q tests/'
      }
    }
  }
  post {
    always { echo "agent_runtime pipeline done" }
  }
}
