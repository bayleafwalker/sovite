from enum import StrEnum


class ResourceOperation(StrEnum):
    KVM_LAB_CREATE = "kvm-lab.create"
    KVM_LAB_OBSERVE = "kvm-lab.observe"
    KVM_LAB_STOP = "kvm-lab.stop"
    MODEL_PEER_HEALTH_READ = "model-peer.health-read"

    def __str__(self) -> str:
        return str(self.value)
