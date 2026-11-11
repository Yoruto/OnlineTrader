"""Records requested effects. It does not match orders or invent broker fills."""

import copy


class RecordingPort:
    def __init__(self):
        self.orders = []
        self.cancellations = []
        self.fail_send = False

    def send(self, request):
        self.orders.append(copy.deepcopy(request))
        if self.fail_send:
            raise RuntimeError("Injected ambiguous send failure")
        return {"stage": "development-call-returned"}

    def cancel(self, request):
        self.cancellations.append(copy.deepcopy(request))
        return {"stage": "development-cancel-call-returned"}
