def lambda_handler(event, context):
    print("Database migration Lambda executed.")
    return {
        "statusCode": 200,
        "body": "Migration Lambda executed successfully."
    }