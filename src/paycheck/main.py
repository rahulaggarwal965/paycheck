"""Main entrypoint for the paycheck calculator using Hydra configuration."""

import hydra
from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError
from .config_models import AppConfig
import sys


@hydra.main(version_base=None, config_path="../../conf", config_name="config")
def main(cfg: DictConfig) -> None:
    """Main function that validates config and runs the paycheck pipeline."""
    # Convert OmegaConf to dict and validate with Pydantic
    try:
        config_dict = OmegaConf.to_container(cfg, resolve=True)
        app_config = AppConfig(**config_dict)
    except ValidationError as e:
        print(f"Configuration validation error:\n{e}")
        sys.exit(1)
    except Exception as e:
        print(f"Configuration error: {e}")
        sys.exit(1)
    
    # Import and run the computation pipeline
    try:
        from .pipeline import run_paycheck_pipeline
        run_paycheck_pipeline(app_config)
    except ImportError as e:
        print(f"Pipeline import error: {e}")
        print("Pipeline not yet implemented. Configuration validation successful.")
    except Exception as e:
        print(f"Pipeline execution error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
