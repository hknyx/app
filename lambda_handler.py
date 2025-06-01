import base64
import shlex
import importlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Tuple
from datetime import datetime
from typing import Any, Dict, List, Type, Union

import boto3
from PIL import Image

s3 = boto3.client("s3")


bedrock_runtime = boto3.client(
    service_name="bedrock-runtime",
    region_name="us-west-2",
)


def retry_with_backoff(func, *args, max_retries=3, initial_delay=1):
    """
    Retry a function with exponential backoff

    Args:
        func: Function to retry
        args: Arguments to pass to the function
        max_retries: Maximum number of retries (default: 3)
        initial_delay: Initial delay in seconds (default: 1)
    """
    for attempt in range(max_retries):
        try:
            result = func(*args)
            if all(r is not None for r in result if isinstance(result, tuple)):
                return result

            # If we get here, some part of the result was None
            print(f"Attempt {attempt + 1} failed with None result")
        except Exception as e:
            print(f"Attempt {attempt + 1} failed with error: {str(e)}")

        if attempt < max_retries - 1:  # Don't sleep on the last attempt
            sleep_time = initial_delay * (2**attempt)  # Exponential backoff
            print(f"Waiting {sleep_time} seconds before retry...")
            time.sleep(sleep_time)

    return None, None  # Return None if all retries failed


def upload_to_s3(file_bytes, file_name):
    """
    Upload a file to S3 and return the URL
    """
    try:
        s3_client = boto3.client("s3")
        bucket_name = os.getenv("S3_BUCKET_NAME")
        # Generate a unique file name to avoid collisions
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        s3_key = f"uploaded_images/{timestamp}_{unique_id}_{file_name}"

        # Upload the file
        content_type = (
            "image/jpeg"
            if file_name.lower().endswith((".jpg", ".jpeg"))
            else "image/png"
        )

        # Convert BytesIO to bytes if necessary
        if isinstance(file_bytes, io.BytesIO):
            file_bytes = file_bytes.getvalue()

        s3_client.put_object(
            Bucket=bucket_name, Key=s3_key, Body=file_bytes, ContentType=content_type
        )

        # Generate the URL
        url = f"https://{bucket_name}.s3.amazonaws.com/{s3_key}"
        return url
    except Exception as e:
        print(f"Error uploading to S3: {str(e)}")
        return None


def call_claude_3_fill(
    system_prompt: str,
    prompt: str,
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0",
):

    prompt_config = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "system": system_prompt,
        "stop_sequences": ["```"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "Here is the code with no explanation ```python",
                    },
                ],
            },
        ],
    }

    body = json.dumps(prompt_config)

    modelId = model_id
    accept = "application/json"
    contentType = "application/json"

    response = bedrock_runtime.invoke_model(
        body=body, modelId=modelId, accept=accept, contentType=contentType
    )
    response_body = json.loads(response.get("body").read())

    results = response_body.get("content")[0].get("text")
    return results


def load_json(path_to_json: str) -> Dict[str, Any]:
    """
    Purpose:
        Load json files
    Args:
        path_to_json (String): Path to  json file
    Returns:
        Conf: JSON file if loaded, else None
    """
    try:
        with open(path_to_json, "r") as config_file:
            conf = json.load(config_file)
            return conf

    except Exception as error:
        logging.error(error)
        raise TypeError("Invalid JSON file")


aws_service_to_module_mapping = load_json("diag_mapping.json")


def validate_python_code(code: str) -> bool:
    """
    Validate that the provided code is safe to execute.
    This is a basic validation - extend as needed for your security requirements.
    
    Args:
        code (str): The Python code to validate
    
    Returns:
        bool: True if code appears safe, False otherwise
    """
    # List of potentially dangerous imports/functions to block
    dangerous_patterns = [
        r'\bos\.system\b',
        r'\bsubprocess\.call\b',
        r'\bsubprocess\.Popen\b',
        r'\beval\b',
        r'\bexec\b',
        r'\b__import__\b',
        r'\bopen\s*\(',
        r'\bfile\s*\(',
        r'\binput\s*\(',
        r'\braw_input\s*\(',
    ]
    
    # Check for dangerous patterns
    for pattern in dangerous_patterns:
        if re.search(pattern, code, re.IGNORECASE):
            print(f"Potentially dangerous pattern found: {pattern}")
            return False
    
    # Only allow specific imports that are known to be safe for diagram generation
    allowed_imports = [
        r'from diagrams import',
        r'from diagrams\.',
        r'import diagrams',
    ]
    
    # Extract import statements
    import_lines = [line.strip() for line in code.split('\n') 
                   if line.strip().startswith(('import ', 'from '))]
    
    for import_line in import_lines:
        is_allowed = any(re.search(pattern, import_line, re.IGNORECASE) 
                        for pattern in allowed_imports)
        if not is_allowed:
            print(f"Potentially unsafe import found: {import_line}")
            return False
    
    return True


def process_code(code):
    # Split the code into lines
    lines = code.split("\n")

    # Initialize variables to store the updated code and diagram filename
    updated_lines = []
    diagram_filename = None
    inside_diagram_block = False

    for line in lines:
        if line == ".":
            line = line.replace(".", "")
        if "endoftext" in line:
            line = ""
        if "# In[" in line:
            line = ""
        if line == "```":
            line = ""

        # Check if the line contains "with Diagram("
        if "with Diagram(" in line:
            # replace / in the line with _
            line = line.replace("/", "_")

            # Extract the diagram name between "with Diagram('NAME',"
            diagram_name = (
                line.split("with Diagram(")[1].split(",")[0].strip("'").strip('"')
            )

            # Convert the diagram name to lowercase, replace spaces with underscores, and add ".png" extension
            diagram_filename = (
                diagram_name.lower()
                .replace(" ", "_")
                .replace(")", "")
                .replace('"', "")
                .replace("/", "_")
                .replace(":", "")
                + ".png"
            )

            # Check if the line contains "filename="
            if "filename=" in line:
                # Extract the filename from the "filename=" parameter
                diagram_filename = (
                    line.split("filename=")[1].split(")")[0].strip("'").strip('"')
                    + ".png"
                )

            inside_diagram_block = True

        # Check if the line contains the end of the "with Diagram:" block
        if inside_diagram_block and line.strip() == "":
            inside_diagram_block = False

        # Only include lines that are inside the "with Diagram:" block or not related to the diagram
        if inside_diagram_block or not line.strip().startswith("diag."):
            updated_lines.append(line)

    # Join the updated lines to create the updated code
    updated_code = "\n".join(updated_lines)

    return updated_code, diagram_filename


def correct_imports(code):
    print("Starting correct_imports function")
    print(f"Original code:\n{code}")

    detected_services = []
    # First, sort services by length (longest first) to avoid partial matches
    sorted_services = sorted(aws_service_to_module_mapping.keys(), key=len, reverse=True)
    
    for service in sorted_services:
        if re.search(r'\b' + re.escape(service) + r'\b', code):
            detected_services.append(service)
            print(f"Detected service: {service} -> {aws_service_to_module_mapping[service]}")

    imports = []
    replacements = {}
    for service in detected_services:
        mapping = aws_service_to_module_mapping[service]
        if isinstance(mapping, str) and '.' in mapping:
            module_parts = mapping.split('.')
            if len(module_parts) >= 4:
                module_path = '.'.join(module_parts[:-1])  # e.g., 'diagrams.aws.database'
                class_name = module_parts[-1]              # e.g., 'Dynamodb'
                
                try:
                    # Import the module to verify the class exists
                    module = importlib.import_module(module_path)
                    if hasattr(module, class_name):
                        # Always use an alias if the service name is different from the actual class name
                        if service != class_name:
                            import_stmt = f"from {module_path} import {class_name} as {service}"
                            replacements[service] = service  # Keep the service name
                        else:
                            import_stmt = f"from {module_path} import {class_name}"
                        
                        imports.append(import_stmt)
                        print(f"Added import: {import_stmt}")
                    else:
                        print(f"Warning: Class {class_name} not found in {module_path}")
                except ImportError as e:
                    print(f"Warning: Could not import {module_path}: {str(e)}")

    # Add imports to the code
    if imports:
        # Add diagrams import first
        final_imports = ["from diagrams import Diagram"]
        final_imports.extend(sorted(set(imports)))
        imports_text = "\n".join(final_imports)
        code = imports_text + "\n\n" + code
        print(f"Final code with imports:\n{code}")
    else:
        print("No imports were generated!")

    return code


def test_dynamodb_import():
    """Test function to verify the imports"""
    test_code = """
with Diagram('Test Architecture', show=False):
    db = DynamoDB('My Table')
    """
    
    print("\nTesting DynamoDB import...")
    result = correct_imports(test_code)
    print("\nResult code:")
    print(result)


def diagram_tool(query):
    """
    This is a tool that generates diagrams based on a customer's request.
    """
    system_prompt = """
    You are an expert python programmer that has mastered the Diagrams library. You are able to write code to generate AWS diagrams based on what the user asks. Only return the code as it will be run through a program to generate the diagram for the user.
    """

    try:
        # Get code from Claude
        raw_code = call_claude_3_fill(system_prompt, query)
        print("Base code:")
        print(raw_code)

        # Clean up and process the code
        processed_code, expected_filename = process_code(raw_code)
        processed_code = processed_code.replace("```python", "").replace("```", "").replace('"""', "")
        final_code = correct_imports(processed_code)

        print("Cleaned code:")
        print(final_code)

        # Validate the code for security
        if not validate_python_code(final_code):
            print("Code validation failed - contains unsafe operations")
            return None, None

        # Generate secure filename and paths
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        secure_filename = f"diagram_{timestamp}_{unique_id}.py"
        file_path = f"/tmp/{secure_filename}"
        
        # Write the validated code to file
        try:
            with open(file_path, "w") as file:
                file.write(final_code)
        except IOError as e:
            print(f"Failed to write code to file: {e}")
            return None, None
        
        # Get Python executable path and validate
        python_executable = sys.executable
        if not os.path.isfile(python_executable):
            print("Python executable not found")
            return None, None
        
        # Execute the code with direct subprocess control
        original_dir = os.getcwd()
        try:
            os.chdir('/tmp')
            
            # SECURITY: Ensure python_executable and file_path are not influenced by untrusted sources
            # If these are derived from user input, sanitize or validate strictly
            allowed_interpreters = ['/usr/bin/python3', '/var/lang/bin/python3.8']
            if python_executable not in allowed_interpreters:
                raise ValueError(f"Disallowed python interpreter: {python_executable}")

             # Sanitize the file path
             if not file_path.startswith('/tmp/') or '..' in file_path:
                raise ValueError("Invalid file path")

        
    
    # Use a list of arguments instead of string
    cmd_list = [python_executable, file_path]
    
    
            
            # Execute with security precautions (no shell, validated inputs)
            result = subprocess.run(
                cmd_list,
                capture_output=True,
                text=True,
                check=True,
                shell=False,
                cwd='/tmp',
                timeout=30,
                env={
                    'PATH': '/usr/bin:/bin',
                    'PYTHONPATH': '/tmp',
                    'HOME': '/tmp'
                }
            )
            
            if result.stdout:
                print(f"Code execution output:\n{result.stdout}")
            if result.stderr:
                print(f"Code execution errors:\n{result.stderr}")
                
        except subprocess.CalledProcessError as e:
            print("Error occurred while running the code:")
            print(f"stdout: {e.stdout}")
            print(f"stderr: {e.stderr}")
            return None, None
        except subprocess.TimeoutExpired:
            print("Code execution timed out")
            return None, None
        finally:
            os.chdir(original_dir)
        
        # Determine output filename
        diagram_filename = expected_filename if expected_filename else "diagram.png"
        
        # Open the generated image
        img_path = f"/tmp/{diagram_filename}"
        if os.path.exists(img_path):
            img = Image.open(img_path)
            return img, diagram_filename
        else:
            print(f"Generated image not found at {img_path}")
            return None, None
            
    except Exception as e:
        print(f"Error in diagram_tool: {str(e)}")
        return None, None


def remove_first_line(text):
    lines = text.split("\n")
    if len(lines) > 1:
        lines = lines[1:]
    return "\n".join(lines)


def lambda_handler(event, context):
    # Print the received event to the logs
    print("Received event: ")
    print(event)

    # Extract the action group, api path, and parameters from the prediction
    actionGroup = event["actionGroup"]
    function = event.get("function", "")
    parameters = event.get("parameters", [])
    inputText = event.get("inputText", "")

    # Generate diagram
    image, file_name = retry_with_backoff(diagram_tool, inputText)

    if image is None or file_name is None:
        return {
            "messageVersion": event["messageVersion"],
            "response": {
                "actionGroup": actionGroup,
                "function": function,
                "functionResponse": {
                    "responseBody": {"TEXT": {"body": "Error generating diagram"}}
                },
            },
        }

    # Convert image to bytes and base64
    img_byte_array = io.BytesIO()
    image.save(img_byte_array, format=image.format or "PNG")
    img_byte_array.seek(0)

    # Upload image to s3
    image_url = upload_to_s3(img_byte_array, file_name)
    if image_url is None:
        return {
            "messageVersion": event["messageVersion"],
            "response": {
                "actionGroup": actionGroup,
                "function": function,
                "functionResponse": {
                    "responseBody": {"TEXT": {"body": "Error uploading to S3"}}
                },
            },
        }

    results = {"image_url": image_url}
    response_body = {"TEXT": {"body": str(results)}}

    # Print the response body to the logs
    print(f"Response body: {response_body}")

    # Create the response
    action_response = {
        "actionGroup": actionGroup,
        "function": function,
        "functionResponse": {"responseBody": response_body},
    }

    api_response = {
        "messageVersion": event["messageVersion"],
        "response": action_response,
    }

    return api_response


if __name__ == "__main__":
    test_dynamodb_import()