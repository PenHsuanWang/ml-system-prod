"""One topology definition for relay and consumer, including dead-letter arguments."""


def build_training_broker(url: str, queue_name: str, ack_timeout_ms: int):
    from taskiq.acks import AcknowledgeType
    from taskiq_aio_pika import AioPikaBroker, Exchange, Queue, QueueType
    if not hasattr(AcknowledgeType, "MANUAL"):
        raise RuntimeError("Installed Taskiq lacks the required manual ACK contract")
    return AioPikaBroker(url, exchange=Exchange(name=queue_name+".exchange", durable=True),
                         task_queues=[Queue(name=queue_name, type=QueueType.QUORUM, durable=True,
                                            routing_key=queue_name,
                                            arguments={"x-consumer-timeout": ack_timeout_ms})], qos=1)
