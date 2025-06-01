import io
import json
import random
from io import BytesIO
import os
import logging
import time
from datetime import datetime, date
from botocore.exceptions import ClientError
from tenacity import retry, stop_after_attempt, wait_exponential
import boto3
import matplotlib.pyplot as plt
import streamlit as st
from PIL import Image
from docx import Document
from PyPDF2 import PdfReader

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize AWS clients
cloudwatch = boto3.client('cloudwatch')
logs = boto3.client('logs')

def ensure_log_stream_exists(log_group_name, log_stream_name):
    """Ensure CloudWatch log stream exists"""
    try:
        # First ensure log group exists
        try:
            logs.create_log_group(logGroupName=log_group_name)
            logger.info(f"Created new log group: {log_group_name}")
        except logs.exceptions.ResourceAlreadyExistsException:
            logger.info(f"Log group already exists: {log_group_name}")
        
        # Then create log stream
        try:
            logs.create_log_stream(
                logGroupName=log_group_name,
                logStreamName=log_stream_name
            )
            logger.info(f"Created new log stream: {log_stream_name}")
            return True
        except logs.exceptions.ResourceAlreadyExistsException:
            logger.info(f"Log stream already exists: {log_stream_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to create log stream: {str(e)}")
            return False
    except Exception as e:
        logger.error(f"Failed to ensure log stream exists: {str(e)}")
        return False

def get_sequence_token(log_group_name, log_stream_name):
    """Get the sequence token for the log stream"""
    try:
        response = logs.describe_log_streams(
            logGroupName=log_group_name,
            logStreamNamePrefix=log_stream_name,
            limit=1
        )
        if response['logStreams']:
            return response['logStreams'][0].get('uploadSequenceToken')
        return None
    except Exception as e:
        logger.error(f"Failed to get sequence token: {str(e)}")
        return None

def serialize_for_cloudwatch(obj):
    """Convert objects to JSON-serializable format"""
    if isinstance(obj, Image.Image):
        return "PIL.Image object"
    elif isinstance(obj, bytes):
        return "<binary data>"
    elif isinstance(obj, (datetime, date)):
        return obj.isoformat()
    elif isinstance(obj, (list, tuple)):
        return [serialize_for_cloudwatch(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: serialize_for_cloudwatch(v) for k, v in obj.items()}
    return str(obj)

def log_bedrock_invocation(model_id, input_data, output_data, duration_ms):
    """Log Bedrock invocation details to CloudWatch"""
    try:
        timestamp = datetime.utcnow()
        log_group_name = "/aws/bedrock/invocations"
        # Replace invalid characters in model_id and create valid log stream name
        safe_model_id = model_id.replace(":", "-").replace("/", "-").replace("*", "-").replace(" ", "-")
        log_stream_name = f"{safe_model_id}/{timestamp.strftime('%Y/%m/%d')}"
        
        # Create the log event
        log_event = {
            "schemaType": "ModelInvocationLog",
            "schemaVersion": "1.0",
            "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "accountId": boto3.client('sts').get_caller_identity()['Account'],
            "region": REGION,
            "requestId": str(random.randint(10000000, 99999999)),
            "operation": "InvokeModel",
            "modelId": model_id,
            "input": {
                "inputContentType": "application/json",
                "inputBodyJson": serialize_for_cloudwatch(input_data)
            },
            "output": {
                "outputContentType": "application/json",
                "outputBodyJson": serialize_for_cloudwatch(output_data)
            },
            "metrics": {
                "durationInMilliseconds": duration_ms,
                "inputTokenCount": len(str(serialize_for_cloudwatch(input_data))),
                "outputTokenCount": len(str(serialize_for_cloudwatch(output_data)))
            }
        }
        
        # Send metrics to CloudWatch
        cloudwatch.put_metric_data(
            Namespace='BedrockInvocations',
            MetricData=[
                {
                    'MetricName': 'InvocationDuration',
                    'Value': duration_ms,
                    'Unit': 'Milliseconds',
                    'Dimensions': [
                        {
                            'Name': 'ModelId',
                            'Value': model_id
                        }
                    ]
                },
                {
                    'MetricName': 'InputTokenCount',
                    'Value': len(str(input_data)),
                    'Unit': 'Count',
                    'Dimensions': [
                        {
                            'Name': 'ModelId',
                            'Value': model_id
                        }
                    ]
                },
                {
                    'MetricName': 'OutputTokenCount',
                    'Value': len(str(output_data)),
                    'Unit': 'Count',
                    'Dimensions': [
                        {
                            'Name': 'ModelId',
                            'Value': model_id
                        }
                    ]
                }
            ]
        )
        
        # Ensure log stream exists
        if ensure_log_stream_exists(log_group_name, log_stream_name):
            # Get sequence token if needed
            sequence_token = get_sequence_token(log_group_name, log_stream_name)
            
            # Send log event to CloudWatch Logs
            try:
                kwargs = {
                    'logGroupName': log_group_name,
                    'logStreamName': log_stream_name,
                    'logEvents': [
                        {
                            'timestamp': int(timestamp.timestamp() * 1000),
                            'message': json.dumps(log_event)
                        }
                    ]
                }
                if sequence_token:
                    kwargs['sequenceToken'] = sequence_token
                    
                logs.put_log_events(**kwargs)
                logger.info("Successfully logged Bedrock invocation")
                
            except logs.exceptions.InvalidSequenceTokenException as e:
                logger.warning("Invalid sequence token, retrying with new token")
                # Get the new sequence token from the exception message
                new_token = str(e).split("The next expected sequenceToken is: ")[1]
                kwargs['sequenceToken'] = new_token
                logs.put_log_events(**kwargs)
                
            except Exception as e:
                logger.error(f"Failed to put log events: {str(e)}")
        
        # Log the full event for detailed analysis
        logger.info(f"Bedrock Invocation Log: {json.dumps(log_event, indent=4)}")
        
    except Exception as e:
        logger.error(f"Failed to log Bedrock invocation: {str(e)}")

# Constants
AGENT_ID = "IJWJWHUA7D"
REGION = "us-west-2"
IMAGE_FOLDER = "images"

# Ensure image folder exists
os.makedirs(IMAGE_FOLDER, exist_ok=True)

# Initialize AWS clients
s3_client = boto3.client("s3")
bedrock_runtime = boto3.client(
    service_name="bedrock-runtime",
    region_name=REGION,
)
bedrock_agent_runtime = boto3.client(
    service_name="bedrock-agent-runtime", 
    region_name=REGION
)
cloudwatch = boto3.client('cloudwatch')
logs = boto3.client('logs')

def ensure_log_group_exists(log_group_name="/aws/bedrock/invocations"):
    """Ensure CloudWatch log group exists and is properly configured"""
    try:
        # Try to create the log group
        try:
            logs.create_log_group(logGroupName=log_group_name)
            logger.info(f"Created new log group: {log_group_name}")
        except logs.exceptions.ResourceAlreadyExistsException:
            logger.info(f"Log group already exists: {log_group_name}")
        
        # Set retention policy to 30 days
        logs.put_retention_policy(
            logGroupName=log_group_name,
            retentionInDays=30
        )
        
        # Add tags
        logs.tag_log_group(
            logGroupName=log_group_name,
            tags={
                'Application': 'AnyCompanyReads',
                'Environment': 'Production',
                'Service': 'BedrockInvocations'
            }
        )
        
        return True
    except Exception as e:
        logger.error(f"Failed to configure log group: {str(e)}")
        return False

# Ensure log group exists on module import
ensure_log_group_exists()

def extract_text_from_docx(file_bytes):
    """Extract text from DOCX file"""
    doc = Document(BytesIO(file_bytes))
    full_text = []
    for para in doc.paragraphs:
        full_text.append(para.text)
    return '\n'.join(full_text)

def extract_text_from_pdf(file_bytes):
    """Extract text from PDF file"""
    pdf = PdfReader(BytesIO(file_bytes))
    text = ''
    for page in pdf.pages:
        text += page.extract_text()
    return text

def process_uploaded_document(uploaded_file):
    """Process uploaded document and extract text"""
    try:
        if uploaded_file.type == "text/plain":
            return uploaded_file.getvalue().decode()
        elif uploaded_file.type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return extract_text_from_docx(uploaded_file.getvalue())
        elif uploaded_file.type == "application/pdf":
            return extract_text_from_pdf(uploaded_file.getvalue())
        else:
            raise ValueError(f"Unsupported file type: {uploaded_file.type}")
    except Exception as e:
        logger.error(f"Error processing document: {str(e)}")
        raise

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    reraise=True
)
def invoke_bedrock_with_retry(client, **kwargs):
    """Invoke Bedrock agent with retry logic"""
    try:
        return client.invoke_agent(**kwargs)
    except ClientError as e:
        if e.response['Error']['Code'] == 'throttlingException':
            logger.warning("Rate limit hit, retrying with backoff...")
            raise
        else:
            raise

def init():
    """Initialize AWS clients"""
    try:
        st.session_state.s3_client = s3_client
        st.session_state.bedrock_runtime = bedrock_runtime
        st.session_state.bedrock_agent_runtime = bedrock_agent_runtime
        return True
    except Exception as e:
        st.error(f"Failed to initialize AWS clients: {str(e)}")
        return False

def generate_random_15digit():
    """Generate random 15-digit number"""
    return ''.join([str(random.randint(0, 9)) for _ in range(15)])

def download_image(url):
    """Download image from S3"""
    try:
        bucket = url.split(".s3.amazonaws.com/")[0].split("//")[1]
        key = url.split(".s3.amazonaws.com/")[1]
        response = s3_client.get_object(Bucket=bucket, Key=key)
        image_data = response["Body"].read()
        image = Image.open(BytesIO(image_data))
        return image
    except Exception as e:
        logger.error(f"Failed to download image: {str(e)}")
        raise

def enhance_prompt_with_context(prompt, context):
    """Enhance the prompt with conversation context"""
    if not context:
        return prompt

    context_text = ""
    if context.get("recent_interactions"):
        context_text += "\nRecent conversation context:\n"
        for interaction in context["recent_interactions"]:
            context_text += f"- {interaction}\n"

    if context.get("last_architecture"):
        context_text += "\nPrevious architecture is available for reference.\n"

    enhanced_prompt = f"""
Context from previous interaction:
{context_text}

Current request:
{prompt}

Note: If this request is about creating a CloudFormation template, analyzing costs, 
optimization, or explaining an architecture, please refer to the most recently discussed architecture.
Consider all previous context when generating the response.
"""
    return enhanced_prompt

def store_observation(content, obs_type="text"):
    """Store observation for final display"""
    if not hasattr(st.session_state, 'current_observations'):
        st.session_state.current_observations = []
    
    st.session_state.current_observations.append({
        "type": obs_type,
        "content": content
    })

def fix_dynamodb_code(code):
    """Fix AWS service references in generated code"""
    # Replace incorrect imports with correct ones
    imports_to_fix = {
        'from diagrams.aws.database import DynamoDB': 'from diagrams.aws.database import Dynamodb',
        'from diagrams.aws.database import Elasticache': 'from diagrams.aws.database import ElastiCache',
        'from diagrams.aws.database import ElastiCache': 'from diagrams.aws.database import ElastiCache',
        'from diagrams.aws.analytics import Elasticsearch': 'from diagrams.aws.analytics import Analytics',
        'from diagrams.aws.analytics import ES': 'from diagrams.aws.analytics import Analytics',
        'from diagrams.aws.network import CloudFront': 'from diagrams.aws.network import CDN',
        'from diagrams.aws.network import ElasticLoadBalancing': 'from diagrams.aws.network import ELB',
        'from diagrams.aws.network import ElasticLoadBalancer': 'from diagrams.aws.network import ELB',
        'from diagrams.aws.network import ALB': 'from diagrams.aws.network import ELB',
        'from diagrams.aws.network import ApiGateway': 'from diagrams.aws.network import APIGateway',
        'from diagrams.aws.management import CloudWatch': 'from diagrams.aws.management import Cloudwatch',
        'from diagrams.aws.integration import EventEngine': 'from diagrams.aws.integration import EventBridge',
        'from diagrams.aws.integration import SQS': 'from diagrams.aws.integration import SQS',
        'from diagrams.aws.integration import SNS': 'from diagrams.aws.integration import SNS',
        'from diagrams.aws.security import SecretsManager': 'from diagrams.aws.security import Secretsmanager',
        'from diagrams.aws.compute import Lambda': 'from diagrams.aws.compute import Lambda',
        'from diagrams.aws.storage import S3': 'from diagrams.aws.storage import S3',
        'from diagrams.aws.database import DynamoDB, RDS': 'from diagrams.aws.database import Dynamodb, RDS',
        'from diagrams.aws.database import RDS, DynamoDB': 'from diagrams.aws.database import RDS, Dynamodb',
        'from diagrams.aws.database import DynamoDB, ElastiCache': 'from diagrams.aws.database import Dynamodb, ElastiCache',
        'from diagrams.aws.database import ElastiCache, DynamoDB': 'from diagrams.aws.database import ElastiCache, Dynamodb'
    }
    
    # Replace service references in the code
    service_mappings = {
        'DynamoDB(': 'Dynamodb(',
        'Elasticache(': 'ElastiCache(',
        'ElastiCache(': 'ElastiCache(',
        'Elasticsearch(': 'Analytics(',
        'ES(': 'Analytics(',
        'CloudWatch(': 'Cloudwatch(',
        'CloudFront(': 'CDN(',
        'Cloudfront(': 'CDN(',
        'ElasticLoadBalancing(': 'ELB(',
        'ElasticLoadBalancer(': 'ELB(',
        'ALB(': 'ELB(',
        'ApiGateway(': 'APIGateway(',
        'EventEngine(': 'EventBridge(',
        'SecretsManager(': 'Secretsmanager(',
        'SQS(': 'SQS(',
        'SNS(': 'SNS(',
        'Lambda(': 'Lambda(',
        'S3(': 'S3('
    }
    
    # First fix the imports
    for old_import, new_import in imports_to_fix.items():
        code = code.replace(old_import, new_import)
    
    # Then fix service references
    for old_service, new_service in service_mappings.items():
        code = code.replace(old_service, new_service)
    
    # Ensure all necessary imports are present
    required_imports = """
from diagrams import Diagram, Cluster
from diagrams.aws.compute import Lambda, ECS, ElasticBeanstalk
from diagrams.aws.database import RDS, ElastiCache, Dynamodb
from diagrams.aws.network import ELB, CDN, Route53, APIGateway
from diagrams.aws.security import Cognito, WAF, Secretsmanager
from diagrams.aws.storage import S3
from diagrams.aws.integration import SQS, SNS, EventBridge
from diagrams.aws.analytics import Analytics, Kinesis
from diagrams.aws.management import Cloudwatch
from diagrams.aws.ml import Personalize
"""
    
    # Add imports if they're not present
    if 'from diagrams' not in code:
        code = required_imports + code
    
    # Additional fixes for variable names and comments
    variable_fixes = {
        'dynamodb = DynamoDB': 'dynamodb = Dynamodb',
        'session_db = DynamoDB': 'session_db = Dynamodb',
        'db = DynamoDB': 'db = Dynamodb',
        'dynamo = DynamoDB': 'dynamo = Dynamodb',
        'ddb = DynamoDB': 'ddb = Dynamodb',
        'dynamoDB = DynamoDB': 'dynamoDB = Dynamodb',
        'DynamoDB_instance = DynamoDB': 'DynamoDB_instance = Dynamodb',
        'dynamodb_table = DynamoDB': 'dynamodb_table = Dynamodb',
        'dynamodb_db = DynamoDB': 'dynamodb_db = Dynamodb'
    }
    
    for old_var, new_var in variable_fixes.items():
        code = code.replace(old_var, new_var)
    
    # Fix any remaining DynamoDB references that might be in comments or strings
    code = code.replace('# DynamoDB', '# Dynamodb')
    code = code.replace('"DynamoDB"', '"Dynamodb"')
    code = code.replace("'DynamoDB'", "'Dynamodb'")
    
    return code

def get_fallback_code(error_message):
    """Generate fallback code when main process fails"""
    fallback_code = """
# Fallback simple architecture
from diagrams import Diagram
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb, RDS
from diagrams.aws.network import APIGateway

with Diagram("Simple Architecture", show=False):
    api = APIGateway("API")
    func = Lambda("Function")
    db = RDS("Database")
    session = Dynamodb("Sessions")
    
    api >> func >> [db, session]
"""
    return fallback_code

def process_aws_query(prompt, uploaded_file=None, previous_context=None):
    """Process AWS-related queries with context awareness and document support"""
    logger.info(f"Processing query: {prompt}")
    start_time = time.time()
    
    try:
        # Generate session ID
        session_id = generate_random_15digit()
        
        # Create trace container for Streamlit
        trace_container = st.container()
        final_container = st.container()

        # Initialize model response
        model_response = {
            "text": "",
            "images": [],
            "files": [],
            "traces": []
        }

        # Clear previous observations
        if hasattr(st.session_state, 'current_observations'):
            st.session_state.current_observations = []

        # Handle uploaded document and enhance prompt
        if uploaded_file and not uploaded_file.type.startswith('image/'):
            try:
                document_text = process_uploaded_document(uploaded_file)
                enhanced_prompt = f"""
Based on the following requirements document:

{document_text}

User request:
{prompt}

Please analyze these requirements and create an appropriate AWS architecture.
"""
                logger.info("Enhanced prompt with document content")
            except Exception as e:
                error_msg = f"Document processing error: {str(e)}"
                logger.error(error_msg)
                return {"text": error_msg, "images": []}
        else:
            # Use original context enhancement for non-document queries
            enhanced_prompt = enhance_prompt_with_context(prompt, previous_context)
            
        logger.info(f"Enhanced prompt with context: {enhanced_prompt}")
        
        # Prepare input data for logging
        input_data = {
            "prompt": enhanced_prompt,
            "sessionId": session_id,
            "agentId": AGENT_ID,
            "context": previous_context
        }
        
        try:
            # Invoke Bedrock agent with retry logic
            response = invoke_bedrock_with_retry(
                bedrock_agent_runtime,
                agentId=AGENT_ID,
                agentAliasId="TSTALIASID",
                sessionId=session_id,
                inputText=enhanced_prompt,
                endSession=False,
                enableTrace=True
            )
            
            # Calculate duration and log the invocation
            duration_ms = int((time.time() - start_time) * 1000)
            log_bedrock_invocation(
                model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                input_data=input_data,
                output_data=response,
                duration_ms=duration_ms
            )
            
        except Exception as e:
            error_msg = "Rate limit exceeded. Please wait a moment before trying again."
            logger.error(f"Bedrock invocation failed: {str(e)}")
            
            # Log the failed invocation
            duration_ms = int((time.time() - start_time) * 1000)
            log_bedrock_invocation(
                model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                input_data=input_data,
                output_data={"error": str(e)},
                duration_ms=duration_ms
            )
            
            st.error(error_msg)
            return {
                "text": "I apologize, but I'm receiving too many requests right now. Please wait a moment and try again.",
                "images": []
            }

        # Get the event stream from the response
        event_stream = response["completion"]

        for index, event in enumerate(event_stream):
            try:
                logger.info(f"Processing event {index}")
                
                # Check trace
                if "trace" in event:
                    if ("trace" in event["trace"] and 
                        "orchestrationTrace" in event["trace"]["trace"]):
                        trace_event = event["trace"]["trace"]["orchestrationTrace"]
                        
                        logger.info(f"Processing trace event type: {trace_event.get('type', 'unknown')}")

                        if "rationale" in trace_event:
                            trace_text = trace_event["rationale"]["text"]
                            trace_object = {"trace_type": "rationale", "text": trace_text}
                            model_response["traces"].append(trace_object)

                            with trace_container.expander("Reasoning", expanded=True):
                                st.markdown(trace_text)
                                store_observation(trace_text)

                        if "invocationInput" in trace_event:
                            if ("codeInterpreterInvocationInput"
                                in trace_event["invocationInput"]):
                                try:
                                    trace_code = trace_event["invocationInput"][
                                        "codeInterpreterInvocationInput"
                                    ]["code"]
                                    
                                    # Apply fixes to the code before it's executed
                                    trace_code = fix_dynamodb_code(trace_code)
                                    logger.info("Applied service name fixes to diagram code")
                                    
                                    # Update the code in the invocation input to ensure fixed version is executed
                                    trace_event["invocationInput"]["codeInterpreterInvocationInput"]["code"] = trace_code
                                    
                                    trace_object = {
                                        "trace_type": "codeInterpreter",
                                        "text": trace_code,
                                    }
                                    model_response["traces"].append(trace_object)

                                    with trace_container.expander("Code", expanded=True):
                                        st.code(trace_code)
                                        store_observation(trace_code, "code")
                                except Exception as e:
                                    logger.error(f"Error processing diagram code: {str(e)}")
                                    # Use fallback code if there's an error
                                    fallback_code = get_fallback_code(str(e))
                                    trace_code = fallback_code
                                    trace_object = {
                                        "trace_type": "codeInterpreter",
                                        "text": trace_code,
                                    }
                                    model_response["traces"].append(trace_object)
                                    with trace_container.expander("Code (Fallback)", expanded=True):
                                        st.code(trace_code)
                                        store_observation(trace_code, "code")

                        if "observation" in trace_event:
                            if ("codeInterpreterInvocationOutput"
                                in trace_event["observation"]):
                                if ("executionOutput"
                                    in trace_event["observation"][
                                        "codeInterpreterInvocationOutput"
                                    ]):
                                    trace_resp = trace_event["observation"][
                                        "codeInterpreterInvocationOutput"
                                    ]["executionOutput"]
                                    trace_object = {
                                        "trace_type": "observation",
                                        "text": trace_resp,
                                    }
                                    model_response["traces"].append(trace_object)

                                    with trace_container.expander("Observation", expanded=True):
                                        st.markdown(trace_resp)
                                        
                                        if "CloudFormation" in trace_resp or "Resources:" in trace_resp:
                                            store_observation(trace_resp, "code")
                                            with final_container:
                                                st.markdown("### 📄 CloudFormation Template")
                                                st.code(trace_resp, language="yaml")
                                        else:
                                            store_observation(trace_resp)
                                        
                                        try:
                                            output_dict = json.loads(trace_resp.replace("'", '"'))
                                            if "image_url" in output_dict:
                                                image = download_image(output_dict["image_url"])
                                                st.image(image, caption="Generated Architecture", use_container_width=True)
                                                model_response["images"].append({
                                                    "image": image,
                                                    "caption": "Generated AWS Architecture",
                                                    "url": output_dict["image_url"]
                                                })
                                        except json.JSONDecodeError:
                                            logger.warning("Failed to parse JSON from trace response")
                                        except Exception as e:
                                            logger.error(f"Error processing image: {str(e)}")

                        if "actionGroupInvocationOutput" in trace_event["observation"]:
                            trace_resp = trace_event["observation"][
                                "actionGroupInvocationOutput"
                            ]["text"]
                            trace_object = {
                                "trace_type": "observation",
                                "text": trace_resp,
                            }
                            model_response["traces"].append(trace_object)

                            with trace_container.expander("Action Output", expanded=True):
                                st.markdown(trace_resp)
                                store_observation(trace_resp)
                                try:
                                    trace_resp_dict = json.loads(trace_resp.replace("'", '"'))
                                    if "image_url" in trace_resp_dict:
                                        image = download_image(trace_resp_dict["image_url"])
                                        st.image(image, caption="Generated Architecture", use_container_width=True)
                                        model_response["images"].append({
                                            "image": image,
                                            "caption": "Generated AWS Architecture",
                                            "url": trace_resp_dict["image_url"]
                                        })
                                except:
                                    pass

                        if "finalResponse" in trace_event["observation"]:
                            trace_resp = trace_event["observation"]["finalResponse"]["text"]
                            model_response["text"] = trace_resp

                            store_observation(trace_resp)

                            with final_container:
                                st.markdown("## 🏗️ Architecture Design")
                                st.markdown(trace_resp)
                                
                                if model_response["images"]:
                                    st.markdown("### Generated Architecture Diagram")
                                    for img_data in model_response["images"]:
                                        st.image(
                                            img_data["image"], 
                                            caption=img_data["caption"],
                                            use_container_width=True
                                        )

                # Handle text chunks
                if "chunk" in event:
                    chunk = event["chunk"]
                    if "bytes" in chunk:
                        text = chunk["bytes"].decode("utf-8")
                        model_response["text"] += text

                # Handle file outputs
                if "files" in event:
                    files = event["files"]["files"]
                    for file in files:
                        name = file["name"]
                        type = file["type"]
                        bytes_data = file["bytes"]

                        if type == "image/png":
                            img = plt.imread(io.BytesIO(bytes_data))
                            img_name = f"{IMAGE_FOLDER}/{name}"
                            plt.imsave(img_name, img)
                            model_response["images"].append({
                                "image": img,
                                "caption": f"Generated: {name}"
                            })
                            logger.info(f"Image '{name}' saved to disk.")

            except Exception as e:
                logger.error(f"Error processing event: {e}")
                continue

        return model_response

    except Exception as e:
        logger.error(f"Error in process_aws_query: {str(e)}")
        try:
            fallback_code = get_fallback_code(str(e))
            logger.info("Attempting fallback code")
            
            with trace_container.expander("Code (Fallback)", expanded=True):
                st.code(fallback_code)
                store_observation(fallback_code, "code")
                
            return {
                "text": "I encountered an issue with the original diagram, so I've created a simplified version. Would you like me to explain the components?",
                "images": [],
                "traces": []
            }
        except Exception as fallback_error:
            logger.error(f"Fallback also failed: {str(fallback_error)}")
            return {
                "text": f"Error processing query: {str(e)}",
                "images": []
            }
