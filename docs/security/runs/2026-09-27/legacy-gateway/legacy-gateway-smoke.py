import os,sys,subprocess,json
os.environ.update(AWS_ACCESS_KEY_ID='synthetic',AWS_SECRET_ACCESS_KEY='synthetic',AWS_EC2_METADATA_DISABLED='true',INPUT_QUEUE_URL='https://sqs.us-east-1.amazonaws.com/000000000000/input',RESPONSE_QUEUE_URL='https://sqs.us-east-1.amazonaws.com/000000000000/output')
sys.path.insert(0,'/app/app')
import sqs_consumer
from botocore.stub import Stubber
with Stubber(sqs_consumer.sqs) as stub:
 stub.add_response('receive_message',{'Messages':[]},{'QueueUrl':os.environ['INPUT_QUEUE_URL'],'MaxNumberOfMessages':1,'WaitTimeSeconds':20,'VisibilityTimeout':900,'AttributeNames':['All']})
 assert sqs_consumer.receive_message()==[]
 stub.assert_no_pending_responses()
assert sqs_consumer.load_session_history('')==[]
version=subprocess.check_output(['aws','--version'],text=True).strip()
assert 'aws-cli/2.37.4 Python/3.14.7' in version
print(json.dumps({'consumer_import':True,'receive_contract':True,'empty_history':True,'aws_cli':version,'uid':os.getuid()}))
