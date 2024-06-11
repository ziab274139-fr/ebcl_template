from ebita.api.test_class_base import TestClassBase
from utils import run_container, run_command, stop_container

class E2e(TestClassBase):
    def setup_class():
        """Run the Docker SDK container."""
        run_container()

    def teardown_class():
        """Stop the Docker SDK container."""
        stop_container()

    def image_using_local_app(self):
        pass
