#
# Author: Hasan Bank
# Email: hasanbank@gmail.com

import os
import numpy as np
from datetime import datetime
from tqdm import tqdm
import time

import argparse
from tensorboardX import SummaryWriter

import torch
import torch.optim as optim
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
import torch.backends.cudnn as cudnn
import torch.nn as nn

import sys
sys.path.append('../')

from utils.ResNet import ResNet50_S1, ResNet50_S2
from utils.dataGenBigEarth import dataGenBigEarthLMDB, ToTensor, Normalize, ConcatDataset
from utils.metrics import MetricTracker, get_k_hamming_neighbours, get_mAP,get_mAP_weighted, timer,\
     calculateAverageMetric


parser = argparse.ArgumentParser(description='PyTorch multi-label Sentinel Images CBIR')
parser.add_argument('--S1LMDBPth', metavar='DATA_DIR',
                        help='path to the saved sentinel 1 LMDB dataset')
parser.add_argument('--S2LMDBPth', metavar='DATA_DIR',
                        help='path to the saved sentinel 2 LMDB dataset')
parser.add_argument('-b', '--batch-size', default=200, type=int,
                        metavar='N', help='mini-batch size (default: 200)')
parser.add_argument('--epochs', type=int, default=500, help='epoch number')
parser.add_argument('--k', type=int, default=20, help='number of retrived images per query')
parser.add_argument('--lr', default=0.001, type=float, help='initial learning rate')
parser.add_argument('--num_workers', default=8, type=int, metavar='N',
                        help='num_workers for data loading in pytorch')
parser.add_argument('--bits', type=int, default=16, help='number of bits to use in hashing')
parser.add_argument('--serbia', dest='serbia', action='store_true',
                    help='use the serbia patches')
parser.add_argument('--train_csvS1', metavar='CSV_PTH',
                        help='path to the csv file of train patches')
parser.add_argument('--val_csvS1', metavar='CSV_PTH',
                        help='path to the csv file of val patches')
parser.add_argument('--test_csvS1', metavar='CSV_PTH',
                        help='path to the csv file of test patches')
parser.add_argument('-loss', '--lossFunction', type=str, dest = 'lossFunc', help="which loss function will be used?", choices=['MSELoss', 'TripletLoss'], default='MSELoss')


args = parser.parse_args()


checkpoint_dir = os.path.join('./', 'Resnet50Pair', 'checkpoints')
logs_dir = os.path.join('./', 'Resnet50Pair', 'logs')
result_dir = os.path.join('./', 'Resnet50Pair', 'results')
dataset_dir = os.path.join('./','Resnet50Pair','dataset')


if not os.path.isdir(checkpoint_dir):
    os.makedirs(checkpoint_dir)
if not os.path.isdir(logs_dir):
    os.makedirs(logs_dir)
if not os.path.isdir(dataset_dir):
    os.makedirs(dataset_dir)
if not os.path.isdir(result_dir):
    os.makedirs(result_dir)
    
    
beta = 0.001
gamma = 1    
    
def write_arguments_to_file(args, filename):
    with open(filename, 'w') as f:
        for key, value in vars(args).items():
            f.write('%s: %s\n' % (key, str(value)))

def save_checkpoint(state, name):
    filename = os.path.join(checkpoint_dir, name + '_checkpoint.pth.tar')
    torch.save(state, filename)


def get_triplets(train_labels):
    indices = get_k_hamming_neighbours(train_labels,train_labels)
    
    anchors = indices[:,0]
    anchors = anchors.reshape(1,-1)
    
    positives = indices[:,1]
    positives = positives.reshape(1,-1)
    
    negatives = indices[:,-1]
    negatives = negatives.reshape(1,-1)
    
    triplets = torch.cat((anchors,positives,negatives))
    
    return triplets


#Binarization Loss
def pushLoss(logitS1, logitS2):
    
    lossDifference = torch.nn.L1Loss(reduction='none')
    
    TensorThreshold = torch.ones_like(logitS1) * 0.5
        
    errorS1 = torch.sum( lossDifference(logitS1,TensorThreshold) ** 2, 1, True  )
    errorS2 = torch.sum( lossDifference(logitS2,TensorThreshold) ** 2, 1, True  ) 

    averageError = (errorS1 + errorS2) / 2
    return torch.mean(averageError)
    
def pushLossInMSE(logitS1_1,logitS1_2, logitS2_1, logitS2_2):
    lossDifference = torch.nn.L1Loss(reduction='none')
    TensorThreshold = torch.ones_like(logitS1_1) * 0.5
        
    errorS1_1 = torch.sum( lossDifference(logitS1_1,TensorThreshold) ** 2, 1, True  )      
    errorS1_2 = torch.sum( lossDifference(logitS1_2,TensorThreshold) ** 2, 1, True  )
    
    errorS2_1 = torch.sum( lossDifference(logitS2_1,TensorThreshold) ** 2, 1, True  ) 
    errorS2_2 = torch.sum( lossDifference(logitS2_2,TensorThreshold) ** 2, 1, True  ) 


    averageError = (errorS1_1 + errorS1_2 + errorS2_1 + errorS2_2) / 4
    return torch.mean(averageError)




    
def balancingLoss(logitS1, logitS2):    
    
    lossDifference = torch.nn.L1Loss(reduction='none')
    
    meanS1 = torch.mean(logitS1,1,True)
    meanS2 = torch.mean(logitS2,1,True)
    
    tensorThreshold = torch.ones_like(meanS1) * 0.5
    
    errorS1 = lossDifference(meanS1,tensorThreshold) **2   
    errorS2 = lossDifference(meanS2,tensorThreshold) **2
    
    averageError = (errorS1 + errorS2) / 2
    return torch.mean(averageError)
    

def balancingLossInMSE(logitS1_1,logitS1_2, logitS2_1, logitS2_2):
    lossDifference = torch.nn.L1Loss(reduction='none')
    
    meanS1_1 = torch.mean(logitS1_1,1,True)
    meanS1_2 = torch.mean(logitS1_2,1,True)

    meanS2_1 = torch.mean(logitS2_1,1,True)
    meanS2_2 = torch.mean(logitS2_2,1,True)

    
    tensorThreshold = torch.ones_like(meanS1_1) * 0.5
    
    errorS1_1 = lossDifference(meanS1_1,tensorThreshold) **2   
    errorS1_2 = lossDifference(meanS1_2,tensorThreshold) **2   

    errorS2_1 = lossDifference(meanS2_1,tensorThreshold) **2   
    errorS2_2 = lossDifference(meanS2_2,tensorThreshold) **2
    
    averageError = (errorS1_1 + errorS1_2 + errorS2_1 + errorS2_2 ) / 4
    return torch.mean(averageError)
    
    
    

def triplet_loss(a, p, n, margin=0.2) : 
    d = nn.PairwiseDistance(p=2)
    distance = d(a, p) - d(a, n) + margin 
    loss = torch.mean(torch.max(distance, torch.zeros_like(distance))) 
    return loss
    



def main():
    global args

    sv_name = datetime.strftime(datetime.now(), '%Y%m%d_%H%M%S')
    sv_name = sv_name + '_' + str(args.bits) + '_' + str(args.k) + '_' + args.lossFunc
    print('saving file name is ', sv_name)

    write_arguments_to_file(args, os.path.join(logs_dir, sv_name+'_arguments.txt'))

    
    resultsFile_name = os.path.join(result_dir, sv_name+'_results.txt')
    

    if args.serbia:

            #Sentinel 2 in Serbia Statistics
            bands_mean = {
                            'bands10_mean': [ 458.93423 ,  676.8278,  665.719, 2590.4482],
                            'bands20_mean': [ 1065.233, 2068.3826, 2435.3057, 2647.92, 2010.1838, 1318.5911],
                            'bands60_mean': [ 341.05457, 2630.7898 ],
                        }
        
            bands_std = {
                            'bands10_std': [ 315.86624,  305.07462,  302.11145, 310.93375],
                            'bands20_std': [ 288.43314, 287.29364, 299.83383, 295.51282, 211.81876,  193.92213],
                            'bands60_std': [ 267.79263, 292.94092 ]
                    }
            #Sentinel 1 in Serbia Statistics
            polars_mean = {
                    'polarVH_mean': [ -15.827944 ],
                    'polarVV_mean': [ -9.317011]
                }

            polars_std = {
                    'polarVH_std': [ 0.782826 ],
                    'polarVV_std': [ 1.8147297]
            }
            
       
    else:

        bands_mean = {
                'bands10_mean': [ 429.9430203 ,  614.21682446,  590.23569706, 2218.94553375],
                'bands20_mean': [ 950.68368468, 1792.46290469, 2075.46795189, 2266.46036911, 1594.42694882, 1009.32729131],
                'bands60_mean': [ 340.76769064, 2246.0605464 ],
                    }
    
        bands_std = {
                'bands10_std': [ 572.41639287,  582.87945694,  675.88746967, 1365.45589904],
                'bands20_std': [ 729.89827633, 1096.01480586, 1273.45393088, 1356.13789355, 1079.19066363,  818.86747235],
                'bands60_std': [ 554.81258967, 1302.3292881 ]
                    }
                
                


    modelS1 = ResNet50_S1(args.bits)
    modelS2 = ResNet50_S2(args.bits)
    
    
    if torch.cuda.is_available():
        torch.backends.cudnn.enabled = True
        cudnn.benchmark = True
        modelS1.cuda()
        modelS2.cuda()
        gpuDisabled = False
    else:
        modelS1.cpu()
        modelS2.cpu()
        gpuDisabled = True
        
    print('GPU Disabled: ',gpuDisabled)



    train_dataGenS1 =  dataGenBigEarthLMDB(
                    bigEarthPthLMDB=args.S1LMDBPth,
                    isSentinel2 = False,
                    state='train',
                    imgTransform=transforms.Compose([
                        ToTensor(isSentinel2 = False),
                        Normalize(polars_mean, polars_std, False)
                    ]),
                    upsampling=False,
                    train_csv=args.train_csvS1,
                    val_csv=args.val_csvS1,
                    test_csv=args.test_csvS1
    )
        
    train_dataGenS2 = dataGenBigEarthLMDB(
                    bigEarthPthLMDB=args.S2LMDBPth,
                    isSentinel2 = True,
                    state='train',
                    imgTransform=transforms.Compose([
                        ToTensor(isSentinel2=True),
                        Normalize(bands_mean, bands_std,True)
                    ]),
                    upsampling=True,
                    train_csv=args.train_csvS1,
                    val_csv=args.val_csvS1,
                    test_csv=args.test_csvS1
    )

    val_dataGenS1 = dataGenBigEarthLMDB(
                    bigEarthPthLMDB=args.S1LMDBPth,
                    isSentinel2 = False,
                    state='val',
                    imgTransform=transforms.Compose([
                        ToTensor(False),
                        Normalize(polars_mean, polars_std,False)
                    ]),
                    upsampling=False,
                    train_csv=args.train_csvS1,
                    val_csv=args.val_csvS1,
                    test_csv=args.test_csvS1
    )
    
    val_dataGenS2 = dataGenBigEarthLMDB(
                    bigEarthPthLMDB=args.S2LMDBPth,
                    isSentinel2 = True,
                    state='val',
                    imgTransform=transforms.Compose([
                        ToTensor(True),
                        Normalize(bands_mean, bands_std,True)
                    ]),
                    upsampling=True,
                    train_csv=args.train_csvS1,
                    val_csv=args.val_csvS1,
                    test_csv=args.test_csvS1
    )
    
    train_data_loader = DataLoader(
            ConcatDataset(train_dataGenS1,train_dataGenS2),
            batch_size=args.batch_size, num_workers=args.num_workers, shuffle=True, pin_memory=True)

    val_data_loader = DataLoader(
            ConcatDataset(val_dataGenS1,val_dataGenS2),
            batch_size=args.batch_size, num_workers=args.num_workers, shuffle=False, pin_memory=True)    
    
    


    optimizerS1 = optim.Adam(modelS1.parameters(), lr=args.lr, weight_decay=1e-4)
    optimizerS2 = optim.Adam(modelS2.parameters(), lr=args.lr, weight_decay=1e-4)



    train_writer = SummaryWriter(os.path.join(logs_dir, 'runs', sv_name, 'training'))
    val_writer = SummaryWriter(os.path.join(logs_dir, 'runs', sv_name, 'val'))


    toWrite = False
    best_averageMAP = 0
    start_epoch = 0

    start = time.time()
    for epoch in range(start_epoch, args.epochs):

        print('Epoch {}/{}'.format(epoch, args.epochs - 1))
        print('-' * 10)

        with open(resultsFile_name, 'a') as resultsFile:
            resultsFile.write('Epoch {}/{}\n'.format(epoch, args.epochs - 1))
            resultsFile.write('-' * 10 + '\n')




        train(train_data_loader, modelS1,modelS2, optimizerS1,optimizerS2, epoch, train_writer,gpuDisabled,resultsFile_name)
                        
        averageMAP,val_S1codes,val_S2codes,label_val,name_valS1,name_valS2 = val(val_data_loader, modelS1,modelS2, optimizerS1,optimizerS2, val_writer,gpuDisabled,resultsFile_name)
        
        val_S1codes = torch.stack(val_S1codes).reshape(len(val_S1codes),args.bits)
        val_S2codes = torch.stack(val_S2codes).reshape(len(val_S2codes),args.bits)
        label_val = torch.stack(label_val,0)




        is_best_acc = averageMAP > best_averageMAP
        best_averageMAP = max(best_averageMAP, averageMAP)
        
        print('is_best_acc: ',is_best_acc)
        
        with open(resultsFile_name, 'a') as resultsFile:
                resultsFile.write( 'Best Epoch: {}\n '.format(is_best_acc))
                

        if is_best_acc:
                            
            generatedS1CodesToWriteFile = []
            generatedS2CodesToWriteFile = []
            trainedLabelsToWriteFile = []
            trainedS1FileNamesToWriteFile = []    
            trainedS2FileNamesToWriteFile = []  
            
            
            generatedS1CodesToWriteFile =  val_S1codes
            generatedS2CodesToWriteFile = val_S2codes
            
            trainedLabelsToWriteFile = label_val
            
            trainedS1FileNamesToWriteFile = name_valS1
            trainedS2FileNamesToWriteFile = name_valS2

            epochToWrite = epoch
            stateS1DictToWrite = modelS1.state_dict()
            stateS2DictToWrite = modelS2.state_dict()

            optimizerS1ToWrite = optimizerS1.state_dict()
            optimizerS2ToWrite = optimizerS2.state_dict()

            bestF1ToWrite = best_averageMAP
            toWrite = True

    end = time.time()
    print('Training and Validation Time has been elapsed')
    print(timer(start,end))
    
    with open(resultsFile_name, 'a') as resultsFile:
        resultsFile.write('Training and Validation Time has been elapsed:{}\n'.format(timer(start,end)))
    
    
    
    if toWrite :
        
        
        dataset_folder = os.path.join(dataset_dir,sv_name)
        if not os.path.isdir(dataset_folder):
            os.makedirs(dataset_folder)
        
        fileGeneratedS1Codes = os.path.join(dataset_folder, 'generatedS1Codes.pt')
        fileGeneratedS2Codes = os.path.join(dataset_folder, 'generatedS2Codes.pt')

        fileTrainedS1Names = os.path.join(dataset_folder, 'trainedS1Names.npy')
        fileTrainedS2Names = os.path.join(dataset_folder, 'trainedS2Names.npy')

        fileTrainedLabels = os.path.join(dataset_folder,'trainedLabels.pt')


        torch.save(generatedS1CodesToWriteFile,fileGeneratedS1Codes)
        torch.save(generatedS2CodesToWriteFile,fileGeneratedS2Codes)
        
        np.save(fileTrainedS1Names, trainedS1FileNamesToWriteFile )
        np.save(fileTrainedS2Names, trainedS2FileNamesToWriteFile )

        torch.save(trainedLabelsToWriteFile, fileTrainedLabels)
        
        save_checkpoint({
            'epoch': epochToWrite,
            'state_dictS1': stateS1DictToWrite,
            'state_dictS2': stateS2DictToWrite,
            'optimizerS1': optimizerS1ToWrite,
            'optimizerS2': optimizerS2ToWrite,
            'best_map': bestF1ToWrite,
        }, sv_name)
        
        
    


def train(trainloader, modelS1,modelS2, optimizerS1,optimizerS2, epoch, train_writer,gpuDisabled,resultsFile_name):

     
    lossTracker = MetricTracker()
    
    modelS1.train()
    modelS2.train()
    
    for idx, (dataS1,dataS2) in enumerate(tqdm(trainloader, desc="training")):
        numSample = dataS2["bands10"].size(0)
        
        if not args.lossFunc == 'TripletLoss':
            lossFunc = nn.MSELoss()
            
            halfNumSample = numSample // 2
            
            if gpuDisabled :
                bands1 = torch.cat((dataS2["bands10"][:halfNumSample], dataS2["bands20"][:halfNumSample],dataS2["bands60"][:halfNumSample]), dim=1).to(torch.device("cpu"))
                polars1 = torch.cat((dataS1["polarVH"][:halfNumSample], dataS1["polarVV"][:halfNumSample]), dim=1).to(torch.device("cpu"))
                labels1 = dataS2["label"][:halfNumSample].to(torch.device("cpu")) 
                
                bands2 = torch.cat((dataS2["bands10"][halfNumSample:], dataS2["bands20"][halfNumSample:],dataS2["bands60"][halfNumSample:]), dim=1).to(torch.device("cpu"))
                polars2 = torch.cat((dataS1["polarVH"][halfNumSample:], dataS1["polarVV"][halfNumSample:]), dim=1).to(torch.device("cpu"))
                labels2 = dataS2["label"][halfNumSample:].to(torch.device("cpu")) 
                
                labels = torch.cat((labels1,labels2)).to(torch.device('cpu'))
                
                onesTensor = torch.ones(halfNumSample)
                
            else:
                bands1 = torch.cat((dataS2["bands10"][:halfNumSample], dataS2["bands20"][:halfNumSample],dataS2["bands60"][:halfNumSample]), dim=1).to(torch.device("cuda"))
                polars1 = torch.cat((dataS1["polarVH"][:halfNumSample], dataS1["polarVV"][:halfNumSample]), dim=1).to(torch.device("cuda"))
                labels1 = dataS2["label"][:halfNumSample].to(torch.device("cuda")) 
                
                bands2 = torch.cat((dataS2["bands10"][halfNumSample:], dataS2["bands20"][halfNumSample:],dataS2["bands60"][halfNumSample:]), dim=1).to(torch.device("cuda"))
                polars2 = torch.cat((dataS1["polarVH"][halfNumSample:], dataS1["polarVV"][halfNumSample:]), dim=1).to(torch.device("cuda"))
                labels2 = dataS2["label"][halfNumSample:].to(torch.device("cuda")) 
                
                labels = torch.cat((labels1,labels2)).to(torch.device('cuda'))
                
                onesTensor = torch.cuda.FloatTensor(halfNumSample).fill_(0)
            
                    
            
            optimizerS1.zero_grad()
            optimizerS2.zero_grad()
            
            logitsS1_1 = modelS1(polars1)
            logitsS1_2 = modelS1(polars2)
            
            logitsS2_1 = modelS2(bands1)
            logitsS2_2 = modelS2(bands2)
    
    
            cos = torch.nn.CosineSimilarity(dim=1)
            cosBetweenLabels = cos(labels1,labels2)
            
            cosBetweenS1 = cos(logitsS1_1,logitsS1_2)
            cosBetweenS2 = cos(logitsS2_1,logitsS2_2)
            
            cosInterSameLabel1 = cos(logitsS1_1,logitsS2_1  )
            cosInterSameLabel2 = cos(logitsS1_2, logitsS2_2)
            
            cosInterDifLabel1 = cos(logitsS1_1,logitsS2_2)
            cosInterDifLabel2 = cos(logitsS1_2,logitsS2_1)
            
        
            S1IntraLoss = lossFunc(cosBetweenS1,cosBetweenLabels)
            S2IntraLoss = lossFunc(cosBetweenS2,cosBetweenLabels)
        
    
            InterLoss_SameLabel1 = lossFunc(cosInterSameLabel1,onesTensor)
            InterLoss_SameLabel2 = lossFunc(cosInterSameLabel2,onesTensor)
            
            InterLoss_DifLabel1 = lossFunc(cosInterDifLabel1,cosBetweenLabels)
            InterLoss_DifLabel2 = lossFunc(cosInterDifLabel2,cosBetweenLabels)
            
            mseLoss = 0.33 * S1IntraLoss + 0.33 * S2IntraLoss + 0.0825 * InterLoss_SameLabel1 + 0.0825 *  InterLoss_SameLabel2 + 0.0825 * InterLoss_DifLabel1 * 0.0825 * InterLoss_DifLabel2
        
            
            pushLossValue = pushLossInMSE(logitsS1_1, logitsS1_2, logitsS2_1, logitsS2_2)
            balancingLossValue = balancingLossInMSE(logitsS1_1, logitsS1_2, logitsS2_1, logitsS2_2)
            
            loss = mseLoss - beta * pushLossValue / args.bits + gamma * balancingLossValue
            
       
        
       
        else:
            if gpuDisabled :
                bands = torch.cat((dataS2["bands10"], dataS2["bands20"],dataS2["bands60"]), dim=1).to(torch.device("cpu"))
                polars = torch.cat((dataS1["polarVH"], dataS1["polarVV"]), dim=1).to(torch.device("cpu"))
                labels = dataS2["label"].to(torch.device("cpu")) 
                
            else:            
                bands = torch.cat((dataS2["bands10"], dataS2["bands20"],dataS2["bands60"]), dim=1).to(torch.device("cuda"))
                polars = torch.cat((dataS1["polarVH"], dataS1["polarVV"]), dim=1).to(torch.device("cuda"))
                labels = dataS2["label"].to(torch.device("cuda")) 
                
            optimizerS1.zero_grad()
            optimizerS2.zero_grad()
            
            logitsS1 = modelS1(polars)
            logitsS2 = modelS2(bands)
            
            
            pushLossValue = pushLoss(logitsS1,logitsS2)
            balancingLossValue = balancingLoss(logitsS1,logitsS2)
            

            triplets = get_triplets(labels)
            
            S1IntraLoss = triplet_loss(logitsS1[triplets[0]], logitsS1[triplets[1]], logitsS1[triplets[2]] )
            S2IntraLoss = triplet_loss(logitsS2[triplets[0]], logitsS2[triplets[1]], logitsS2[triplets[2]] )
            
            InterLoss1 = triplet_loss(logitsS1[triplets[0]], logitsS2[triplets[1]], logitsS2[triplets[2]] )
            InterLoss2 = triplet_loss(logitsS2[triplets[0]], logitsS1[triplets[1]], logitsS1[triplets[2]] )

            tripletLoss = 0.25 * S1IntraLoss + 0.25 * S2IntraLoss + 0.25 * InterLoss1 + 0.25 * InterLoss2
            
            
            loss = tripletLoss - beta * pushLossValue / args.bits + gamma * balancingLossValue
            
                    
            
        loss.backward()
        optimizerS1.step()
        optimizerS2.step()
        

        lossTracker.update(loss.item(), numSample)


    train_writer.add_scalar("loss", lossTracker.avg, epoch)

    print('Train loss: {:.6f}'.format(lossTracker.avg))
    with open(resultsFile_name, 'a') as resultsFile:
        resultsFile.write('Train loss: {:.6f}\n'.format(lossTracker.avg))

    
    

def val(valloader, modelS1,modelS2, optimizerS1,optimizerS2, val_writer, gpuDisabled,resultsFile_name):


    modelS1.eval()
    modelS2.eval()

    label_val = []
    predicted_S1codes = []
    predicted_S2codes = []
    name_valS1 = []
    name_valS2 = []
    
    mapS1toS1 = 0
    mapS1toS2 = 0
    mapS2toS1 = 0
    mapS2toS2 = 0
    
    mapS1toS1_weighted = 0
    mapS1toS2_weighted = 0
    mapS2toS1_weighted = 0
    mapS2toS2_weighted = 0

    
    
    totalSize = 0 

    with torch.no_grad():
        for batch_idx, (dataS1,dataS2) in enumerate(tqdm(valloader, desc="validation")):

            totalSize += dataS2["bands10"].size(0)
            
            if gpuDisabled:
                bands = torch.cat((dataS2["bands10"], dataS2["bands20"],dataS2["bands60"]), dim=1).to(torch.device("cpu"))
                polars = torch.cat((dataS1["polarVH"], dataS1["polarVV"]), dim=1).to(torch.device("cpu"))
                labels = dataS1["label"].to(torch.device("cpu"))  
            else:
                bands = torch.cat((dataS2["bands10"], dataS2["bands20"],dataS2["bands60"]), dim=1).to(torch.device("cuda"))
                polars = torch.cat((dataS1["polarVH"], dataS1["polarVV"]), dim=1).to(torch.device("cuda"))
                labels = dataS1["label"].to(torch.device("cuda")) 
                

            logitsS1 = modelS1(polars)
            logitsS2 = modelS2(bands)

            
            binaryS1 = (torch.sign(logitsS1 - 0.5) + 1 ) / 2
            binaryS2 = (torch.sign(logitsS2 - 0.5) + 1 ) / 2

            predicted_S1codes += list(binaryS1)
            predicted_S2codes += list(binaryS2)
                        
            label_val += list(labels)
            name_valS1 += list(dataS1['patchName'])
            name_valS2 += list(dataS2['patchName'])
            
    
    
    
    
    valCodesS1 = torch.stack(predicted_S1codes).reshape(len(predicted_S1codes),args.bits)
    valCodesS2 = torch.stack(predicted_S2codes).reshape(len(predicted_S2codes),args.bits)
    valLabels = torch.stack(label_val).reshape(len(label_val),len(label_val[0]))
    
    for i in range(len(valCodesS1)):
        
        queryCodeS1 = valCodesS1[i].reshape(1,-1)
        queryCodeS2 = valCodesS2[i].reshape(1,-1)
        queryLabel = label_val[i]
                
        databaseS1 = valCodesS1
        databaseS2 = valCodesS2
        databaseLabels = valLabels
        
        databaseS1 = torch.cat([databaseS1[0:i], databaseS1[i+1:]])
        databaseS2 = torch.cat([databaseS2[0:i], databaseS2[i+1:]])
        databaseLabels = torch.cat([databaseLabels[0:i], databaseLabels[i+1:]])
        
        #S1 to S1
        neighboursIndices = get_k_hamming_neighbours(databaseS1, queryCodeS1)  
        mapPerBatch = get_mAP(neighboursIndices,args.k,databaseLabels,queryLabel)
        mapPerBatch_Weighted = get_mAP_weighted(neighboursIndices,args.k,databaseLabels,queryLabel)
        mapS1toS1 += mapPerBatch
        mapS1toS1_weighted += mapPerBatch_Weighted
        
        #S1 to S2
        neighboursIndices = get_k_hamming_neighbours(databaseS2,queryCodeS1)  
        mapPerBatch = get_mAP(neighboursIndices,args.k,databaseLabels,queryLabel)
        mapPerBatch_weighted = get_mAP_weighted(neighboursIndices,args.k,databaseLabels,queryLabel)
        mapS1toS2 += mapPerBatch
        mapS1toS2_weighted += mapPerBatch_weighted

        
            
        #S2 to S1
        neighboursIndices = get_k_hamming_neighbours(databaseS1,queryCodeS2)  
        mapPerBatch = get_mAP(neighboursIndices,args.k,databaseLabels,queryLabel)
        mapPerBatch_weighted = get_mAP_weighted(neighboursIndices,args.k,databaseLabels,queryLabel)
        mapS2toS1 += mapPerBatch
        mapS2toS1_weighted += mapPerBatch_weighted
                  
        #S2 to S2
        neighboursIndices = get_k_hamming_neighbours(databaseS2, queryCodeS2)  
        mapPerBatch = get_mAP(neighboursIndices,args.k,databaseLabels,queryLabel)
        mapPerBatch_weighted = get_mAP_weighted(neighboursIndices,args.k,databaseLabels,queryLabel)
        mapS2toS2 += mapPerBatch
        mapS2toS2_weighted += mapPerBatch_weighted


    mapS1toS1 = calculateAverageMetric(mapS1toS1,totalSize)
    mapS1toS2 = calculateAverageMetric(mapS1toS2,totalSize)
    mapS2toS1 = calculateAverageMetric(mapS2toS1,totalSize)
    mapS2toS2 = calculateAverageMetric(mapS2toS2,totalSize)
    averageMap = (mapS1toS1 + mapS1toS2 + mapS2toS1 + mapS2toS2 ) / 4   
    
    mapS1toS1_weighted = calculateAverageMetric(mapS1toS1_weighted,totalSize)
    mapS1toS2_weighted = calculateAverageMetric(mapS1toS2_weighted,totalSize)
    mapS2toS1_weighted = calculateAverageMetric(mapS2toS1_weighted,totalSize)
    mapS2toS2_weighted = calculateAverageMetric(mapS2toS2_weighted,totalSize)
    averageMap_weighted = (mapS1toS1_weighted + mapS1toS2_weighted + mapS2toS1_weighted + mapS2toS2_weighted ) / 4 

    print('# Roy mAP Calculations #')
    print('MaP for S1 to S1: ', mapS1toS1)
    print('MaP for S1 to S2: ', mapS1toS2)
    print('MaP for S2 to S1: ', mapS2toS1)
    print('MaP for S2 to S2: ', mapS2toS2)
    print('Average mAP@',args.k,':{0}'.format(averageMap))
    
    print('# Weighted mAP Calculations #')
    print('MaP for S1 to S1: ', mapS1toS1_weighted)
    print('MaP for S1 to S2: ', mapS1toS2_weighted)
    print('MaP for S2 to S1: ', mapS2toS1_weighted)
    print('MaP for S2 to S2: ', mapS2toS2_weighted)
    print('Average Weighted mAP@',args.k,':{0}'.format(averageMap_weighted))
    
    
    
    
    with open(resultsFile_name, 'a') as resultsFile:
         resultsFile.write('# Roy mAP Calculations #\n')
         resultsFile.write("mAP S1-S1: {}\n ".format(mapS1toS1))
         resultsFile.write("mAP S1-S2: {}\n ".format(mapS1toS2))
         resultsFile.write("mAP S2-S1: {}\n ".format(mapS2toS1))
         resultsFile.write("mAP S2-S2: {}\n ".format(mapS2toS2))
         resultsFile.write("Average mAP@{}: {}\n ".format(args.k, averageMap))
         
         resultsFile.write('# Weighted mAP Calculations #\n')
         resultsFile.write("mAP S1-S1: {}\n ".format(mapS1toS1_weighted))
         resultsFile.write("mAP S1-S2: {}\n ".format(mapS1toS2_weighted))
         resultsFile.write("mAP S2-S1: {}\n ".format(mapS2toS1_weighted))
         resultsFile.write("mAP S2-S2: {}\n ".format(mapS2toS2_weighted))
         resultsFile.write("Average mAP@{}: {}\n ".format(args.k, averageMap_weighted))
         
         
         

    return (averageMap,predicted_S1codes,predicted_S2codes,label_val,name_valS1,name_valS2)
    
    

if __name__ == "__main__":
    main()
    
    
 
    
    
    
    