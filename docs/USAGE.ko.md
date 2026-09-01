[English](USAGE.md) | **한국어**

# 실행 가이드 (USAGE)

이 문서는 `canvas-pipeline` 코드의 **상세 실행 방법과 주의사항**을 다룹니다.
전체 배경·전략은 [메인 README](../README.ko.md)를 참고하세요.

---

## 워크플로 한눈에

```
[현업 / Canvas]                 [ML팀 / 이 코드]
전처리(flow) + 모델 학습      →    ① promote_model.py    : 모델 승인 + 배포 (버전당 1회)
Model Registry 등록(Pending)    ② inference_pipeline.py : 전처리 + 배치 추론 (신규 데이터마다)
```

승인/배포와 추론을 **분리**한 이유: 실행 시점(트리거)이 다르기 때문입니다.
승인·배포는 새 모델 버전마다 1회, 추론은 신규 데이터마다 반복됩니다.

---

## 파일 구성

| 파일 | 역할 | 실행 빈도 |
|---|---|---|
| `config.py` | 공통 설정(역할·버킷·컨테이너·모델명). 환경변수로 교체 가능 | - |
| `promote_model.py` | **[Flow 1]** 모델 승인 + 배포(Model 객체 생성) | 새 버전마다 1회 |
| `inference_pipeline.py` | **[Flow 2]** 전처리 + 배치 추론 파이프라인 | 신규 데이터마다 |

---

## 최초 준비 — flow · 버킷 구성 (ML팀, 1회)

파이프라인을 처음 세팅할 때, 실행 전에 아래를 **한 번** 수행합니다.
(이후 신규 데이터가 올 때는 이 준비 없이 파이프라인만 반복 실행됩니다.)

### 1) Canvas에서 전처리 flow를 S3로 export
- 현업이 Data Wrangler에서 전처리를 구성한 뒤, Destination(S3) 노드에서 **Export to Amazon S3**를 선택합니다.
- 이 방식은 `.flow`를 코드로 "번역"하지 않고 **그대로 실행**하므로 현업 화면 결과와 100% 일치합니다.

**⚠️ Export job settings — 중요**
- **`Auto job configuration`을 끕니다(비활성화).**
  켜두면 Canvas가 잡 설정을 자동으로 정하는데, 이 경우 EMR Serverless 등으로 실행돼
  우리가 재사용하려는 **SageMaker Processing Job 형태**가 되지 않을 수 있습니다.
- 비활성화하면 실행 엔진 선택지가 나타나며, **`SageMaker Processing`을 선택**합니다.
  (다른 선택지 `EMR Serverless`가 아니라 SageMaker Processing 이어야, 이 저장소의
  `Processor`(Data Wrangler 컨테이너) 기반 파이프라인과 동일한 방식으로 재현됩니다.)
- 여기서 지정하는 **Instance type / count, IAM Role**이 곧 프로세싱 잡 설정이며,
  `.flow`의 `internal_metadata.dw_job`에 기록되어 파이프라인 코드(`config.DW_CONTAINER`,
  Processor 인스턴스 설정)와 일치시켜야 합니다.

> 정리: **Auto job configuration OFF → SageMaker Processing 선택**. 이렇게 export 해야
> ML팀이 `.flow`를 SageMaker Processing Job / Pipeline으로 그대로 재실행할 수 있습니다.

### 2) 프로세싱 잡 디테일 확인 (SageMaker AI 콘솔)
Export 하면 **SageMaker Processing Job이 하나 생성**됩니다. 콘솔에서 그 잡의 설정을 확인합니다.

- SageMaker AI 콘솔 → 좌측 패널 **Data preparation → Processing jobs**
- 목록에서 방금 생성된 잡(이름 예: `canvas-dw-export-s3-<timestamp>`)을 클릭 → 상세에서 아래를 확보:

| 확보할 정보 | 콘솔 위치 (Processing job 상세) | 쓰이는 곳 |
|---|---|---|
| **Image URI** (Data Wrangler 컨테이너) | App specification / Container image | `config.DW_CONTAINER` |
| **flow 입력 S3 URI** | Processing input (name = `flow`) | 아래 4)에서 `.flow` 다운로드 위치 |
| **Instance type / count** | Resources configuration | `Processor` 인스턴스 설정 |
| **IAM Role ARN** | Role | `config.ROLE` 참고 |
| **출력 S3 경로** | Processing output configuration | (참고) |

- 같은 값 중 일부는 `.flow` 내부에도 있습니다:
  `internal_metadata.dw_job.container_uri`, `dw_job.instance_type`.
- **`DEST_OUTPUT_NAME`** 는 `.flow`의 DESTINATION 노드에서 확보합니다
  (`node_id` + `outputs[].name` → `{node_id}.{output}`). 빠르게 확인:
```bash
jq '.nodes[] | {type, node_id, name}' your.flow
jq '.internal_metadata.dw_job' your.flow
```

### 3) S3 버킷 구성 (버전관리 친화적 구조)
```
s3://<bucket>/pipeline_01/
├── raw/loans/
│   ├── incoming/part-1/      # 신규 추론 데이터 도착 위치 (타깃 없음)
│   └── v1/part-1/, v1/part-2/ # flow SOURCE 가 읽는 위치
├── flows/loans-join/         # 편집한 .flow 보관 (Git/버전관리)
└── runs/<execution_id>/      # 실행별 전처리·피처·예측·결과 (자동 생성)
```
버킷 **버전관리** 활성화 권장(실수 삭제/덮어쓰기 대비):
```bash
aws s3api put-bucket-versioning --bucket <bucket> \
  --versioning-configuration Status=Enabled
```

### 4) flow 파일 다운로드 · 구조 이해 · 편집 · 재업로드

**(a) 다운로드** — 2)에서 확인한 flow 입력 S3 경로에서 `.flow`를 받습니다:
```bash
aws s3 cp s3://<원본경로>/xxxxx.flow ./edited_version1.flow
```

**(b) 구조 이해** — `.flow`는 JSON이며 대략 이런 형태입니다(주석은 설명용):
```jsonc
{
  "nodes": [
    { "node_id": "d8651ed4-...", "type": "SOURCE",
      "operator": "sagemaker.s3_source_0.1",
      "parameters": { "dataset_definition": {
        "datasetSourceType": "Canvas Dataset",              // ← "S3" 로 변경
        "s3ExecutionContext": { "s3Uri": "s3://.../part-1.csv" },  // ← 새 입력 경로로 변경
        "canvasDatasetMetadata": { "datasetId": "..." }     // ← null 로 변경
      }}},
    { "type": "TRANSFORM", "operator": "...infer_and_cast_type..." },   // 타입 캐스팅
    { "type": "TRANSFORM", "operator": "...join_tables...",             // 조인
      "parameters": { "join_type": "fullouter", "left_column": "id", "right_column": "id" }},
    { "node_id": "a19ccb7b-...", "type": "DESTINATION",
      "parameters": { "output_config": { "output_path": "s3://.../" }}}  // ← 새 출력 경로로 변경
  ],
  "internal_metadata": {
    "dw_job": { "container_uri": "....dkr.ecr...", "instance_type": "ml.m5.4xlarge" }
  }
}
```

**(c) 수정 포인트**
| 대상 | 필드 | 변경 내용 |
|---|---|---|
| SOURCE (part-1, part-2 **각각**) | `s3ExecutionContext.s3Uri` | 새 입력 경로(`raw/loans/v1/part-1/`, `part-2/`) |
| SOURCE (part-1, part-2 **각각**) | `datasetSourceType` | `"Canvas Dataset"` → `"S3"` |
| SOURCE (part-1, part-2 **각각**) | `canvasDatasetMetadata` | 객체 → `null` (등록 데이터셋 조회 대신 원본 S3 직접 읽기) |
| DESTINATION | `output_config.output_path` | 새 출력 경로 |

**(d) 재업로드** — 편집한 `.flow`를 버킷의 `flows/` 위치에 올립니다:
```bash
aws s3 cp edited_version1.flow \
  s3://<bucket>/pipeline_01/flows/loans-join/edited_version1.flow
```

> ⚠️ 경로 오타 시 프로세싱 잡이 `PATH_NOT_FOUND`로 실패합니다. 업로드 후 SOURCE
> 경로에 실제 데이터가 있는지 `aws s3 ls`로 확인하세요. SOURCE는 **두 개(part-1/part-2)**
> 이니 둘 다 빠짐없이 수정하세요.

---

## 사전 준비

```bash
# AWS 자격증명 (사용 환경에 맞게)
export AWS_PROFILE=<your-profile>
export AWS_DEFAULT_REGION=us-east-1

# 최신 SageMaker SDK 권장 (ModelStep/스케줄 등 최신 기능)
python3 -m pip install -U sagemaker
```

값 교체는 `config.py`를 수정하거나 환경변수로 덮어씁니다(코드 수정 불필요):
```bash
export SM_EXECUTION_ROLE="arn:aws:iam::...:role/MyMLTeamRole"
export SM_BUCKET="my-bucket"
export SM_MODEL_NAME="my-model"
```

---

## 1) 모델 승인 + 배포 — `promote_model.py`

새 모델 버전이 Registry에 등록되면 **1회** 실행합니다.

```bash
python promote_model.py <model_package_arn>
# 예:
python promote_model.py \
  arn:aws:sagemaker:us-east-1:111122223333:model-package/my-model-group/1
```

- 하는 일: (1) 패키지를 `Approved`로 변경 → (2) `config.MODEL_NAME` 이름의 Model 객체 생성
- **비용**: Model 객체는 메타데이터라 **비용 0**. (Canvas의 실시간 Deploy와 달리 유휴 과금 없음)
- 멱등: 이미 승인된 패키지면 승인은 건너뜁니다. 재실행 시 기존 Model은 지우고 재생성합니다.

---

## 2) 배치 추론 — `inference_pipeline.py`

신규 데이터가 준비되면 실행합니다.

```bash
python inference_pipeline.py
```

- `upsert()`로 파이프라인 정의 등록(무료) → `start()`로 실제 실행(과금)
- 흐름: `PreprocessNewData`(flow 재실행) → `BatchPredict`(배치 추론) → `predictions/` 저장
- 실행 중에만 인스턴스가 뜨고, 끝나면 자동 종료(유휴 비용 없음)

---

## 꼭 알아야 할 주의사항

### (1) 입력 경로는 flow 안에 정의되어 있다
전처리 스텝은 `.flow` 파일만 마운트하고, **실제 원본 데이터는 flow의 SOURCE `s3Uri`에서**
Data Wrangler 컨테이너가 직접 읽습니다. 현재 flow의 입력 경로:
```
s3://<BUCKET>/pipeline_01/raw/loans/v1/part-1/
s3://<BUCKET>/pipeline_01/raw/loans/v1/part-2/
```
→ 새 데이터로 추론하려면 **위 경로에 새 파일을 덮어써야** 합니다.
매번 다른 경로를 쓰려면 flow의 SOURCE `s3Uri`를 파라미터화해야 합니다.

### (2) 두 파일 JOIN 타이밍
이 flow는 part-1 + part-2를 `id`로 조인합니다. 자동 트리거 시 한 파일만 도착한
상태로 실행되지 않도록, 두 파일 업로드 후 `_SUCCESS` 완료신호 파일을 올리고
**그 파일 도착만 트리거로** 삼는 것을 권장합니다.

### (3) DW 출력 이름 형식 — `{node_id}.{output_name}`
Data Wrangler 잡은 프로세싱 출력 이름이 flow의 DESTINATION 노드와 일치하기를
요구합니다. 아무 이름을 쓰면 다음 에러로 실패합니다:
```
ProcessingOutputs have invalid names ... {node id}.{output name}
```
`inference_pipeline.py`의 `DEST_OUTPUT_NAME`이 그 값입니다.
**flow를 바꾸면 이 값도 함께 바꿔야** 합니다.

확인 방법: `.flow` 파일에서 `"type":"DESTINATION"` 노드의 `node_id`와 `outputs` 이름을 봅니다.

### (4) 비용 정리
| 대상 | 비용 |
|---|---|
| Model 객체(배포) | 무료 (메타데이터) |
| 파이프라인 정의 / Registry | 무료 |
| Batch Transform / Processing 실행 | **실행 시간만** 과금, 종료 시 인스턴스 자동 회수 |
| S3 결과물 | 저장 비용만 |

---

## 자동화 (선택)

- **정기 배치**: SageMaker Pipelines의 `PipelineSchedule`(빌트인)로 cron/rate 실행
  ```python
  from sagemaker.workflow.triggers import PipelineSchedule
  schedule = PipelineSchedule(name="daily", cron="0 2 * * ? *")
  pipeline.put_triggers(triggers=[schedule], role_arn=ROLE)
  ```
- **도착 즉시**: S3 이벤트 → EventBridge 규칙 → 파이프라인 `StartPipelineExecution`
  (EventBridge가 SageMaker 파이프라인을 네이티브 타깃으로 지원 → Lambda 불필요)
- **승인→배포 자동화**: Registry 승인 이벤트(EventBridge) → `promote_model.py`의
  `deploy()` 로직을 Lambda로 실행

---

## 권한 체크리스트 (config.ROLE)

- 대상 버킷(`SM_BUCKET`) S3 읽기/쓰기
- `iam:PassRole` (잡에 실행 역할 전달)
- `sagemaker:CreateProcessingJob`, `CreateTransformJob`, `CreateModel`, `DeleteModel`
- `sagemaker:UpdateModelPackage`, `DescribeModelPackage` (승인)
