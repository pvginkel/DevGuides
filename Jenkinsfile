// Builds dev-guides.home, every guide of every source repo with the hub's index page and root
// llms.txt, into the dev-guides-home image, and pins it into DevGuidesDeploy, which Argo CD syncs to
// prd.
//
// The guides' checks, tools/check_guide.py over every source repo, run inside the image build
// (Dockerfile), so a guide that fails them never becomes an image and the job needs no stage of its
// own for them. One guide's failure blocks the publish of all of them.
//
// Controller config:
//   - Job: DevGuides
//   - SCM: pvginkel/DevGuides, branch main
//   - Script Path: Jenkinsfile

library identifier: 'JenkinsPipelineUtils', changelog: false

pipeline {
    agent {
        kubernetes {
            inheritFrom 'jenkins-agent kaniko'
            yamlMergeStrategy merge()
            yaml podYaml(templates: ['k8s'])
        }
    }

    options {
        disableConcurrentBuilds(abortPrevious: true)
        skipDefaultCheckout()
        timeout(time: 60, unit: 'MINUTES')
        timestamps()
    }

    triggers {
        githubPush()
    }

    stages {
        stage('Checkout') {
            steps {
                dir('DevGuides') {
                    checkout scm
                }
                // The other repos on sources.txt, where build_hub.py looks for them. These clones
                // are also what rebuild the site when a guide changes: a push to a repo the job's
                // last build checked out starts the job, so the source repos need no job of their
                // own.
                dir('DevGuidesPrivate') {
                    git url: 'https://github.com/pvginkel/DevGuidesPrivate.git', branch: 'main',
                        credentialsId: '5f6fbd66-b41c-405f-b107-85ba6fd97f10'
                }
                dir('JenkinsPipelineUtils') {
                    git url: 'https://github.com/pvginkel/JenkinsPipelineUtils.git', branch: 'main',
                        credentialsId: '5f6fbd66-b41c-405f-b107-85ba6fd97f10'
                }
                dir('ModernAppTemplate') {
                    git url: 'https://github.com/pvginkel/ModernAppTemplate.git', branch: 'main',
                        credentialsId: '5f6fbd66-b41c-405f-b107-85ba6fd97f10'
                }
            }
        }

        stage('Build dev-guides-home image') {
            steps {
                container('kaniko') {
                    script {
                        // The context is the whole workspace: the image builds from every repo
                        // cloned above, side by side.
                        helmCharts.kaniko2(dockerfile: 'DevGuides/Dockerfile', context: '.', destinations: [
                            "registry:5000/dev-guides-home:${currentBuild.number}",
                            'registry:5000/dev-guides-home:latest',
                        ])
                    }
                }
            }
        }

        stage('Write image pins') {
            steps {
                container('k8s') {
                    script {
                        cicd.writeVersionPins(repo: 'pvginkel/DevGuidesDeploy', pins: [
                            'config/prd/values.yaml': ['images.devGuides': ":${currentBuild.number}"],
                        ])
                    }
                }
            }
        }
    }
}
