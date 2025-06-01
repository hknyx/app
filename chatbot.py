import streamlit as st
import boto3
import os
import logging
from datetime import datetime
import uuid
import json
import base64
import requests
from PIL import Image
from io import BytesIO
from docx import Document
from PyPDF2 import PdfReader
from architecture_template import create_architecture_document

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
S3_BUCKET_NAME = "mybuckbuck3"
AGENT_ID = "IJWJWHUA7D"
REGION = "us-west-2"

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

# Load agent tools
try:
    import agent2_tools as agent_tools
    AGENT_AVAILABLE = True
except ImportError:
    try:
        from drawings import agent2_tools as agent_tools
        AGENT_AVAILABLE = True
    except ImportError:
        logger.warning("agent_tools not available")
        AGENT_AVAILABLE = False

# Sample questions dictionary
SAMPLE_QUESTIONS = {
    "Architecture Design": [
        "Draw an AWS diagram that shows an ecommerce architecture",
        "Draw AWS architecture including all the services for this architecture?",
        "Create an architecture diagram for a serverless web application?"
    ],
    "Templates & Analysis": [
        "Create Cloud Formation template for this architecture including all the services and test it?",
        "Calculate estimated cost for this architecture?",
        "Analyze this architecture and provide cost optimization opportunities?",
        "Explain the attached architecture?"
    ]
}

def initialize_session_state():
    """Initialize session state variables"""
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "processing_question" not in st.session_state:
        st.session_state.processing_question = False
    if "doc_url" not in st.session_state:
        st.session_state.doc_url = None
    if "architecture_data" not in st.session_state:
        st.session_state.architecture_data = None
    if "last_response" not in st.session_state:
        st.session_state.last_response = ""
    if "cloudformation_template" not in st.session_state:
        st.session_state.cloudformation_template = None
    if "context" not in st.session_state:
        st.session_state.context = {
            "last_architecture": None,
            "recent_interactions": [],
            "last_template": None,
            "last_response": ""
        }
    if "brd_content" not in st.session_state:
        st.session_state.brd_content = None
    if "s3_client" not in st.session_state:
        st.session_state.s3_client = s3_client
    if "bedrock_runtime" not in st.session_state:
        st.session_state.bedrock_runtime = bedrock_runtime
    if "bedrock_agent_runtime" not in st.session_state:
        st.session_state.bedrock_agent_runtime = bedrock_agent_runtime

def get_proper_service_name(service_name):
    """Convert service names to their proper diagram library equivalents"""
    service_mappings = {
        'DynamoDB': 'Dynamodb',
        'Elasticache': 'ElastiCache',
        'ElastiCache': 'ElastiCache',
        'ElasticSearch': 'Analytics',
        'Elasticsearch': 'Analytics',
        'ES': 'Analytics',
        'CloudWatch': 'Cloudwatch',
        'ElasticLoadBalancing': 'ELB',
        'ElasticLoadBalancer': 'ELB',
        'ALB': 'ELB',
        'ApiGateway': 'APIGateway',
        'CloudFront': 'CDN',
        'Cloudfront': 'CDN',
        'CloudFormation': 'Cloudformation',
        'EventEngine': 'EventBridge',
        'SecretsManager': 'Secretsmanager',
        'SQS': 'SQS',
        'SNS': 'SNS',
        'Lambda': 'Lambda',
        'S3': 'S3'
    }
    return service_mappings.get(service_name, service_name)

def sanitize_diagram_code(code_text):
    """Sanitize the diagram generation code to use proper service names"""
    import_fixes = {
        'from diagrams.aws.analytics import Elasticsearch': 'from diagrams.aws.analytics import Analytics',
        'from diagrams.aws.analytics import ES': 'from diagrams.aws.analytics import Analytics',
        'from diagrams.aws.database import DynamoDB': 'from diagrams.aws.database import Dynamodb',
        'from diagrams.aws.database import Elasticache': 'from diagrams.aws.database import ElastiCache',
        'from diagrams.aws.database import ElastiCache': 'from diagrams.aws.database import ElastiCache',
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
        'from diagrams.aws.storage import S3': 'from diagrams.aws.storage import S3'
    }

    service_fixes = {
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

    for old_import, new_import in import_fixes.items():
        code_text = code_text.replace(old_import, new_import)
    
    for old_service, new_service in service_fixes.items():
        code_text = code_text.replace(old_service, new_service)
    
    return code_text

def is_follow_up_question(prompt):
    """Determine if the prompt is a follow-up question"""
    follow_up_indicators = [
        "this", "that", "the", "these", "those", "it", "previous",
        "above", "existing", "current", "mentioned", "created",
        "architecture", "diagram", "template", "cost", "yes", "no"
    ]
    prompt_lower = prompt.lower()
    return any(indicator in prompt_lower for indicator in follow_up_indicators)

def add_aws_style():
    """Add AWS styling to the app"""
    st.markdown("""
        <style>
        .stButton {
            margin: 0.2rem 0;
        }
        .stButton > button {
            width: 100%;
            text-align: left;
            padding: 0.5rem 1rem;
            background-color: #ff9900;
            color: black;
            border: none;
            border-radius: 4px;
        }
        .stButton > button:hover {
            background-color: #ffac33;
        }
        .category-header {
            font-weight: bold;
            color: #232f3e;
            margin: 1rem 0 0.5rem 0;
        }
        </style>
    """, unsafe_allow_html=True)

def add_aws_banner():
    """Add AWS banner to the Streamlit app"""
    st.markdown("""
        <div style="background-color: #232F3E; padding: 15px; border-radius: 5px; margin-bottom: 20px">
            <img src="https://d1.awsstatic.com/logos/aws-logo-lockups/poweredbyaws/PB_AWS_logo_RGB_stacked_REV_SQ.91cd4af40773cbfbd15577a3c2b8a346fe3e8fa2.png" 
                 alt="AWS Logo" style="height: 40px;">
        </div>
    """, unsafe_allow_html=True)
def get_conversation_context():
    """Get recent conversation context from session state"""
    if not hasattr(st.session_state, 'context'):
        st.session_state.context = {
            "last_architecture": None,
            "recent_interactions": [],
            "last_template": None,
            "brd_content": None
        }
    
    # Ensure BRD content is included in context if available
    if hasattr(st.session_state, 'brd_content') and st.session_state.brd_content:
        st.session_state.context["brd_content"] = st.session_state.brd_content
    
    if st.session_state.messages:
        recent_messages = st.session_state.messages[-3:]
        for msg in recent_messages:
            if isinstance(msg["content"], dict):
                if "text" in msg["content"]:
                    st.session_state.context["recent_interactions"].append(msg["content"]["text"])
                if "images" in msg["content"] and msg["content"]["images"]:
                    st.session_state.context["last_architecture"] = msg["content"]["images"]
                if "traces" in msg["content"]:
                    for trace in msg["content"]["traces"]:
                        if isinstance(trace, dict) and "text" in trace:
                            if "Resources:" in trace["text"]:
                                st.session_state.context["last_template"] = trace["text"]
    
    # Keep only the most recent interactions
    st.session_state.context["recent_interactions"] = st.session_state.context["recent_interactions"][-5:]
    return st.session_state.context

def display_message(message):
    """Display message content including text and images"""
    content = message["content"]
    if isinstance(content, dict):
        if "text" in content:
            text = content["text"]
            if not text.startswith("```") and "use the drawlambda function" not in text.lower() and "i apologize" not in text.lower():
                st.markdown(text)
        
        if "images" in content and content["images"]:
            for image in content["images"]:
                try:
                    if isinstance(image, dict):
                        if "image" in image:
                            with st.container():
                                st.image(image["image"],
                                       caption=image.get("caption", "Generated Architecture"),
                                       use_container_width=True)
                        elif "base64" in image:
                            image_bytes = base64.b64decode(image["base64"])
                            image_data = BytesIO(image_bytes)
                            with st.container():
                                st.image(image_data, use_container_width=True)
                        elif "url" in image:
                            with st.container():
                                st.image(image["url"], use_container_width=True)
                    else:
                        with st.container():
                            st.image(image, use_container_width=True)
                except Exception as e:
                    logger.error(f"Failed to display image: {str(e)}")
        
        if "traces" in content and content["traces"]:
            for trace in content["traces"]:
                if isinstance(trace, dict) and "text" in trace:
                    if "Resources:" in trace["text"]:
                        st.markdown("### CloudFormation Template")
                        st.code(trace["text"], language="yaml")
                        st.session_state.cloudformation_template = trace["text"]
    else:
        if not str(content).startswith("```") and "use the drawlambda function" not in str(content).lower():
            st.markdown(str(content))

def process_query(prompt, uploaded_file=None, maintain_context=True):
    """Process user query through agent with context awareness"""
    if not AGENT_AVAILABLE:
        st.error("Query processing unavailable - agent_tools not loaded")
        return {"text": "Agent tools not available", "images": []}

    try:
        # Get context and ensure it includes BRD content if available
        context = get_conversation_context() if maintain_context else {}
        if st.session_state.brd_content:
            context["brd_content"] = st.session_state.brd_content

        # Handle CloudFormation template requests
        if "cloudformation template" in prompt.lower() or "cf template" in prompt.lower():
            if context and "last_architecture" in context and context["last_architecture"]:
                enhanced_prompt = f"""
Based on the existing architecture, please generate a CloudFormation template that includes all necessary resources and their configurations.

"""
                response = agent_tools.process_aws_query(
                    enhanced_prompt,
                    uploaded_file,
                    previous_context=context
                )
                
                if isinstance(response, dict) and "text" in response:
                    return response
                return {"text": "Failed to generate CloudFormation template. Please try again."}
            return {"text": "No architecture found. Please generate an architecture diagram first."}

        # Handle regular queries and architecture generation
        # If we have BRD content and this is an architecture request, enhance the prompt
        if context.get("brd_content") and ("architecture" in prompt.lower() or "diagram" in prompt.lower()):
            enhanced_prompt = f"""
Based on the following requirements document:

{context['brd_content']}

User request:
{prompt}

Please analyze these requirements and create an appropriate AWS architecture by only using the service lists in diag_mapping.json file.
"""
            response = agent_tools.process_aws_query(
                enhanced_prompt,
                uploaded_file,
                previous_context=context
            )
        else:
            response = agent_tools.process_aws_query(
                prompt,
                uploaded_file,
                previous_context=context
            )
        
        if isinstance(response, dict):
            if "images" in response and response["images"]:
                st.session_state.context["last_architecture"] = response["images"]
            if "traces" in response:
                for trace in response["traces"]:
                    if isinstance(trace, dict) and "text" in trace:
                        trace["text"] = sanitize_diagram_code(trace["text"])
                        if "Resources:" in trace["text"]:
                            st.session_state.context["last_template"] = trace["text"]
            return response
        return {"text": str(response), "images": []}

    except Exception as e:
        logger.error(f"Error in process_query: {str(e)}")
        return {"text": f"Error processing query: {str(e)}", "images": []}

def main():
    try:
        st.set_page_config(page_title="AWS Architecture Designer", layout="wide")
    except:
        pass

    add_aws_style()
    initialize_session_state()

    try:
        boto3.client('sts').get_caller_identity()
    except Exception as e:
        st.error(f"❌ AWS access failed: {str(e)}")

    left_col, right_col = st.columns([1, 3])

    with left_col:
        add_aws_banner()
        
        st.markdown("### 📤 Upload Architecture or Requirements")
        uploaded_file = st.file_uploader(
            "Upload document or image", 
            type=["txt", "doc", "docx", "pdf", "png", "jpg", "jpeg"]
        )

        if uploaded_file is not None:
            if uploaded_file.type.startswith('image/'):
                st.image(uploaded_file, caption="Uploaded Image", use_container_width=True)
                if st.button("📊 Analyze Architecture"):
                    with st.spinner("Analyzing architecture..."):
                        response = process_query(
                            "Please analyze this architecture diagram and explain its components and design.",
                            uploaded_file,
                            maintain_context=False
                        )
                        st.session_state.messages.append({"role": "assistant", "content": response})
                        st.rerun()
            else:
                st.success(f"✅ Uploaded: {uploaded_file.name}")
                try:
                    doc_content = agent_tools.process_uploaded_document(uploaded_file)
                    st.session_state.brd_content = doc_content
                    st.info("BRD document loaded successfully!")
                    
                    if st.button("🏗️ Generate Architecture from BRD"):
                        with st.spinner("Analyzing BRD and generating architecture..."):
                            # Use the same process_query function as the right side
                            maintain_context = True  # We want to maintain context for BRD processing
                            prompt = "Please analyze the requirements document and create an AWS architecture diagram by using the services in the diag_mapping.json file"
                            
                            # Add the BRD content to the conversation context
                            if "context" not in st.session_state:
                                st.session_state.context = {}
                            st.session_state.context["brd_content"] = st.session_state.brd_content
                            
                            # Process using the same function as the chat interface
                            response = process_query(
                                prompt,
                                uploaded_file=uploaded_file,  # Pass the uploaded file
                                maintain_context=maintain_context
                            )
                            
                            # Update session state
                            st.session_state.last_response = response.get("text", "")
                            st.session_state.messages.append({"role": "assistant", "content": response})
                        st.rerun()
                except Exception as e:
                    st.error(f"Error processing document: {str(e)}")
                    st.session_state.brd_content = None

        if st.session_state.cloudformation_template:
            st.markdown("### ⬇️ Download Template")
            st.download_button(
                label="Download CloudFormation Template",
                data=st.session_state.cloudformation_template,
                file_name="architecture_template.yaml",
                mime="text/yaml"
            )

    with right_col:
        st.title("🤖 AWS Architecture Assistant")

        chat_container = st.container()
        with chat_container:
            if "messages" in st.session_state:
                for message in st.session_state.messages:
                    with st.chat_message(message["role"]):
                        display_message(message)

        if prompt := st.chat_input("Ask about AWS architecture..."):
            with st.chat_message("user"):
                st.markdown(prompt)
            st.session_state.messages.append({"role": "user", "content": prompt})
            
            with st.spinner("Processing..."):
                maintain_context = is_follow_up_question(prompt)
                response = process_query(prompt, maintain_context=maintain_context)
                st.session_state.last_response = response.get("text", "")
                st.session_state.messages.append({"role": "assistant", "content": response})
            st.rerun()

        st.markdown("### 💡 Quick Actions")
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("#### Architecture Design")
            for question in SAMPLE_QUESTIONS["Architecture Design"]:
                if st.button(f"🎯 {question[:40]}...", key=f"btn_{hash(question)}"):
                    with st.spinner("Processing..."):
                        response = process_query(question, maintain_context=False)
                        st.session_state.messages.append({"role": "assistant", "content": response})
                    st.rerun()

        with col2:
            st.markdown("#### Analysis & Templates")
            for question in SAMPLE_QUESTIONS["Templates & Analysis"]:
                if st.button(f"📊 {question[:40]}...", key=f"btn_{hash(question)}"):
                    with st.spinner("Processing..."):
                        response = process_query(question, maintain_context=True)
                        st.session_state.messages.append({"role": "assistant", "content": response})
                    st.rerun()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        st.error(f"Application error: {str(e)}")