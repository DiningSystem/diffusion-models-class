import wandb
import numpy as np
import torch, torchvision
import torch.nn.functional as F
from PIL import Image
from tqdm.auto import tqdm
from fastcore.script import call_parse
from torchvision import transforms
from diffusers import DDPMPipeline, UNet2DModel
from diffusers import DDIMScheduler
from datasets import load_dataset
from matplotlib import pyplot as plt
from accelerate import Accelerator
@call_parse
def train(
    image_size = 256,
    batch_size = 16,
    grad_accumulation_steps = 2,
    num_epochs = 1,
    start_model = "google/ddpm-bedroom-256",
    dataset_name = "huggan/wikiart",
    device='cuda',
    model_save_name='wikiart_1e',
    wandb_project='dm_finetune',
    log_samples_every = 250,
    save_model_every = 2500,
    ):
        
    accelerator = Accelerator()
    # Initialize wandb for logging
    wandb.init(project=wandb_project, config=locals())

    unet = UNet2DModel.from_pretrained(start_model, use_auth_token=True)
    sampling_scheduler = DDIMScheduler.from_pretrained(start_model, use_auth_token=True)
    sampling_scheduler.set_timesteps(num_inference_steps=500)
    # Prepare pretrained model
    #image_pipe = DDPMPipeline(unet, sampling_scheduler)
    #image_pipe = DDPMPipeline.from_pretrained(start_model, use_auth_token=True)
    #torch.cuda.set_device(0)
    #torch.cuda.set_device(1)
    #image_pipe= torch.nn.parallel.DistributedDataParallel(image_pipe, device_ids = [0, 1])
    #image_pipe.to(device)
    
    # Get a scheduler for sampling
    #sampling_scheduler = DDIMScheduler.from_config(start_model, use_auth_token=True)
    #sampling_scheduler.set_timesteps(num_inference_steps=500)

    # Prepare dataset
    dataset = load_dataset(dataset_name, split="train")
    preprocess = transforms.Compose(
        [
            transforms.Resize((image_size, image_size)),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5]),
        ]
    )
    def transform(examples):
        images = [preprocess(image.convert("RGB")) for image in examples["image"]]
        return {"images": images}
    dataset.set_transform(transform)
    train_dataloader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=True)

    # Optimizer & lr scheduler
    optimizer = torch.optim.AdamW(unet.parameters(), lr=1e-5)
    scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.9)
    unet, sampling_scheduler, optimizer, train_dataloader, scheduler = accelerator.prepare(
          unet, sampling_scheduler, optimizer, train_dataloader, scheduler
      )
    image_pipe = DDPMPipeline(unet, sampling_scheduler)
    for epoch in range(num_epochs):
        for step, batch in tqdm(enumerate(train_dataloader), total=len(train_dataloader)):

            # Get the clean images
            clean_images = batch['images']

            # Sample noise to add to the images
            noise = torch.randn(clean_images.shape).to(clean_images.device)
            bs = clean_images.shape[0]

            # Sample a random timestep for each image
            timesteps = torch.randint(0, image_pipe.scheduler.num_train_timesteps, (bs,), device=clean_images.device).long()

            # Add noise to the clean images according to the noise magnitude at each timestep
            # (this is the forward diffusion process)
            noisy_images = image_pipe.scheduler.add_noise(clean_images, noise, timesteps)

            # Get the model prediction for the noise
            noise_pred = image_pipe.unet(noisy_images, timesteps, return_dict=False)[0]

            # Compare the prediction with the actual noise:
            loss = F.mse_loss(noise_pred, noise)
            
            # Log the loss
            wandb.log({'loss':loss.item()})

            # Calculate the gradients
            #loss.backward()
            accelerator.backward(loss)

            # Gradient Acccumulation: Only update every grad_accumulation_steps 
            if (step+1)%grad_accumulation_steps == 0:
                optimizer.step()
                optimizer.zero_grad()
                
            # Occasionally log samples
            if (step+1)%log_samples_every == 0:
                x = torch.randn(8, 3, 256, 256).to(device) # Batch of 8
                for i, t in tqdm(enumerate(sampling_scheduler.timesteps)):
                    model_input = sampling_scheduler.scale_model_input(x, t)
                    with torch.no_grad():
                        noise_pred = image_pipe.unet(model_input, t)["sample"]
                    x = sampling_scheduler.step(noise_pred, t, x).prev_sample
                grid = torchvision.utils.make_grid(x, nrow=4)
                im = grid.permute(1, 2, 0).cpu().clip(-1, 1)*0.5 + 0.5
                im = Image.fromarray(np.array(im*255).astype(np.uint8))
                wandb.log({'Sample generations': wandb.Image(im)})
                
            # Occasionally save model
            if (step+1)%save_model_every == 0:
                image_pipe.save_pretrained(start_model)
                #image_pipe.save_pretrained(str(model_save_name)+f'step_{step+1}',push_to_hub=True,re)
                #image_pipe.push_to_hub(model_save_name+f'step_{step+1}', "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")

        # Update the learning rate for the next epoch
        scheduler.step()

    # Save the pipeline one last time
    image_pipe.save_pretrained(start_model)
    #image_pipe.push_to_hub(start_model, "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")
    # Wrap up the run
    wandb.finish()
