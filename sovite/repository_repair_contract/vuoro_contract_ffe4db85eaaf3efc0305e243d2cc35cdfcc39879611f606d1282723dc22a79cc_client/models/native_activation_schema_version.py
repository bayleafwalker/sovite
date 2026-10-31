from enum import StrEnum


class NativeActivationSchemaVersion(StrEnum):
    SOVITE_NATIVE_ACTIVATIONV1 = "sovite-native-activation/v1"

    def __str__(self) -> str:
        return str(self.value)
